"""Anonymous workspace sessions identified by a cookie token, with idle and absolute expiry."""

import hashlib
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .context import ProjectContext
from .settings import Settings
from .storage import BlobStore, ConflictError, NotFoundError, StoredEntity, TableStore

SESSION_ROW = "session"
TOUCH_INTERVAL = timedelta(seconds=60)
MAX_TOKEN_LENGTH = 128
_MAX_UPDATE_ATTEMPTS = 5


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def session_key(token: str) -> str:
    """Storage key for a cookie token, so stored data never reveals usable tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


def session_blob_prefix(key: str) -> str:
    """Return the blob name prefix under which a session's artifacts are stored."""
    return f"sessions/{key}/"


@dataclass
class WorkspaceSession:
    """State of one workspace session, stored as the `SESSION_ROW` entity of its partition.

    Attributes:
        key: Session key (hashed token); also the table partition for the session's data.
        created_at: Creation time; bounds the absolute session lifetime.
        last_seen_at: Last recorded activity; bounds the idle lifetime.
        context: Project context entered by the user, if any.
        context_version: Incremented on every context save.
        sources_version: Incremented whenever the set of sources changes.
        approved_sources_version: `sources_version` at the last approval; -1 when never approved.
        next_source_number: Number to assign to the next added source.
        model_tokens_used: Total language model tokens consumed by the session.
        etag: Etag of the stored entity this instance was read from or written as.
    """

    key: str
    created_at: datetime
    last_seen_at: datetime
    context: ProjectContext | None
    context_version: int
    sources_version: int
    approved_sources_version: int
    next_source_number: int
    model_tokens_used: int = 0
    etag: str = ""

    @property
    def sources_approved(self) -> bool:
        """Whether the current set of sources has been approved."""
        return self.approved_sources_version == self.sources_version

    def to_data(self) -> dict[str, object]:
        """Serialize to table entity data (without key and etag)."""
        return {
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "context": self.context.to_json() if self.context else None,
            "context_version": self.context_version,
            "sources_version": self.sources_version,
            "approved_sources_version": self.approved_sources_version,
            "next_source_number": self.next_source_number,
            "model_tokens_used": self.model_tokens_used,
        }

    @classmethod
    def from_stored(cls, key: str, stored: StoredEntity) -> "WorkspaceSession":
        """Build a session from a stored entity, applying defaults for missing fields.

        Args:
            key: Session key the entity was stored under.
            stored: Entity data and etag as read from the table.

        Returns:
            The session, carrying the entity's etag.
        """
        data = stored.data
        context = data.get("context")
        return cls(
            key=key,
            created_at=datetime.fromisoformat(data["created_at"]),
            last_seen_at=datetime.fromisoformat(data["last_seen_at"]),
            context=ProjectContext.from_json(context) if context else None,
            context_version=int(data.get("context_version", 0)),
            sources_version=int(data.get("sources_version", 0)),
            approved_sources_version=int(data.get("approved_sources_version", -1)),
            next_source_number=int(data.get("next_source_number", 1)),
            model_tokens_used=int(data.get("model_tokens_used", 0)),
            etag=stored.etag,
        )


class SessionService:
    """Create, load, update and delete workspace sessions.

    A session expires after `session_idle_seconds` without activity or `session_max_seconds`
    after creation, whichever comes first. Expired sessions are treated as missing.

    Args:
        table: Table that holds one partition per session.
        blobs: Blob store that holds session artifacts.
        settings: Settings providing the idle and maximum session lifetimes.
        clock: Returns the current time; injectable for tests.
    """

    def __init__(
        self,
        table: TableStore,
        blobs: BlobStore,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._table = table
        self._blobs = blobs
        self._idle = timedelta(seconds=settings.session_idle_seconds)
        self._max = timedelta(seconds=settings.session_max_seconds)
        self._clock = clock

    def create(self) -> tuple[str, WorkspaceSession]:
        """Create a new empty session with a random token.

        Returns:
            The secret cookie token (only its hash is stored) and the new session.

        Raises:
            ConflictError: A session with the same key already exists.
        """
        token = secrets.token_urlsafe(32)
        now = self._clock()
        session = WorkspaceSession(
            key=session_key(token),
            created_at=now,
            last_seen_at=now,
            context=None,
            context_version=0,
            sources_version=0,
            approved_sources_version=-1,
            next_source_number=1,
        )
        session.etag = self._table.insert(session.key, SESSION_ROW, session.to_data())
        return token, session

    def load(self, token: str | None) -> WorkspaceSession | None:
        """Return the active session for a cookie token.

        Args:
            token: Token from the session cookie; missing or overlong tokens are rejected.

        Returns:
            The session, or None when the token is invalid or the session is missing or expired.
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        return self.get_active(session_key(token))

    def get_active(self, key: str) -> WorkspaceSession | None:
        """Return the session for a key, or None when missing or expired.

        An expired session is deleted, including its data and artifacts.

        Args:
            key: Session key.

        Returns:
            The active session, or None.
        """
        stored = self._table.get(key, SESSION_ROW)
        if stored is None:
            return None
        session = WorkspaceSession.from_stored(key, stored)
        if self.is_expired(session):
            self.delete(key)
            return None
        return session

    def is_expired(self, session: WorkspaceSession) -> bool:
        """Whether the session has passed its idle or absolute expiry time."""
        return self._clock() >= self.expires_at(session)

    def expires_at(self, session: WorkspaceSession) -> datetime:
        """Return the earlier of the idle expiry and the absolute expiry time."""
        return min(session.last_seen_at + self._idle, session.created_at + self._max)

    def touch(self, session: WorkspaceSession) -> WorkspaceSession:
        """Record activity to extend the idle expiry.

        Writes at most once per `TOUCH_INTERVAL`; conflicts and missing sessions are ignored.

        Args:
            session: Session that was just used.

        Returns:
            The updated session, or `session` unchanged when no write was made.
        """
        now = self._clock()
        if now - session.last_seen_at < TOUCH_INTERVAL:
            return session

        def mark_seen(current: WorkspaceSession) -> None:
            current.last_seen_at = now

        try:
            return self.mutate(session.key, mark_seen)
        except (ConflictError, NotFoundError):
            return session

    def mutate(self, key: str, change: Callable[[WorkspaceSession], None]) -> WorkspaceSession:
        """Apply `change` to a session with optimistic concurrency.

        `change` is re-run on a freshly read session whenever the write hits an etag conflict.

        Args:
            key: Session key.
            change: Mutates the session in place.

        Returns:
            The updated session.

        Raises:
            NotFoundError: The session does not exist or has expired.
            ConflictError: The write kept conflicting after several attempts.
        """
        for _ in range(_MAX_UPDATE_ATTEMPTS):
            stored = self._table.get(key, SESSION_ROW)
            if stored is None:
                raise NotFoundError("Session not found")
            session = WorkspaceSession.from_stored(key, stored)
            if self.is_expired(session):
                raise NotFoundError("Session expired")
            change(session)
            try:
                session.etag = self._table.update(key, SESSION_ROW, session.to_data(), stored.etag)
            except ConflictError:
                continue
            return session
        raise ConflictError("Session is busy; try again")

    def save_context(self, key: str, context: ProjectContext) -> WorkspaceSession:
        """Store the project context and bump the context version.

        Args:
            key: Session key.
            context: New project context.

        Returns:
            The updated session.

        Raises:
            NotFoundError: The session does not exist or has expired.
            ConflictError: The session stayed busy after several attempts.
        """

        def apply(session: WorkspaceSession) -> None:
            session.context = context
            session.context_version += 1

        return self.mutate(key, apply)

    def allocate_source_number(self, key: str) -> int:
        """Reserve the next source number and bump the sources version.

        Args:
            key: Session key.

        Returns:
            The allocated source number, unique within the session.

        Raises:
            NotFoundError: The session does not exist or has expired.
            ConflictError: The session stayed busy after several attempts.
        """
        allocated: list[int] = []

        def apply(session: WorkspaceSession) -> None:
            allocated[:] = [session.next_source_number]
            session.next_source_number += 1
            session.sources_version += 1

        self.mutate(key, apply)
        return allocated[0]

    def bump_sources_version(self, key: str) -> None:
        """Mark the sources as changed, which invalidates any approval.

        Missing or expired sessions are ignored.

        Args:
            key: Session key.

        Raises:
            ConflictError: The session stayed busy after several attempts.
        """

        def apply(session: WorkspaceSession) -> None:
            session.sources_version += 1

        with suppress(NotFoundError):
            self.mutate(key, apply)

    def approve_sources(self, key: str) -> WorkspaceSession:
        """Approve the current set of sources.

        Args:
            key: Session key.

        Returns:
            The updated session.

        Raises:
            NotFoundError: The session does not exist or has expired.
            ConflictError: The session stayed busy after several attempts.
        """

        def apply(session: WorkspaceSession) -> None:
            session.approved_sources_version = session.sources_version

        return self.mutate(key, apply)

    def add_model_tokens(self, key: str, tokens: int) -> None:
        """Add to the session's language model token usage.

        Missing or expired sessions are ignored.

        Args:
            key: Session key.
            tokens: Number of tokens consumed.

        Raises:
            ConflictError: The session stayed busy after several attempts.
        """

        def apply(session: WorkspaceSession) -> None:
            session.model_tokens_used += tokens

        with suppress(NotFoundError):
            self.mutate(key, apply)

    def delete(self, key: str) -> None:
        """Delete a session with all of its table entities and blobs."""
        # Remove the session row first so access is denied even if later steps fail.
        self._table.delete(key, SESSION_ROW)
        for row, _entity in self._table.list_partition(key):
            self._table.delete(key, row)
        self._blobs.delete_prefix(session_blob_prefix(key))
