"""Add, track and manage the source documents of a workspace session.

Sources are table entities in the session's partition; their original bytes, markdown and
chunks are blobs under the session's prefix. Fetching and conversion run as background jobs.
"""

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .inspection import SourceError, inspect_content
from .sessions import SessionService, WorkspaceSession, session_blob_prefix, utcnow
from .settings import Settings
from .storage import BlobStore, ConflictError, JobQueue, NotFoundError, StoredEntity, TableStore
from .url_fetch import parse_url

SOURCE_ROW_PREFIX = "source:"
MAX_TITLE_LENGTH = 200
_MAX_UPDATE_ATTEMPTS = 5


class SourceKind(StrEnum):
    """How a source was added: as an uploaded file or as a web address."""

    FILE = "file"
    URL = "url"


class SourceStatus(StrEnum):
    """Processing state of a source."""

    QUEUED = "queued"
    FETCHING = "fetching"
    CONVERTING = "converting"
    CONVERTED = "converted"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = frozenset({SourceStatus.QUEUED, SourceStatus.FETCHING, SourceStatus.CONVERTING})
RETRYABLE_STATUSES = frozenset({SourceStatus.FAILED, SourceStatus.CANCELLED})


def source_row(source_id: str) -> str:
    """Return the table row key of a source."""
    return f"{SOURCE_ROW_PREFIX}{source_id}"


def source_blob_prefix(session_key: str, source_id: str) -> str:
    """Return the blob prefix that holds all blobs of a source."""
    return f"{session_blob_prefix(session_key)}sources/{source_id}/"


def original_blob(session_key: str, source_id: str) -> str:
    """Return the blob name of a source's uploaded or downloaded bytes."""
    return f"{source_blob_prefix(session_key, source_id)}original"


def chunks_blob(session_key: str, source_id: str) -> str:
    """Return the blob name of a source's citable chunks (JSON)."""
    return f"{source_blob_prefix(session_key, source_id)}chunks.json"


def markdown_blob(session_key: str, source_id: str) -> str:
    """Return the blob name of a source's converted markdown."""
    return f"{source_blob_prefix(session_key, source_id)}document.md"


def clean_title(value: str) -> str:
    """Drop non-printable characters, collapse whitespace and cut to `MAX_TITLE_LENGTH`."""
    printable = "".join(character for character in value if character.isprintable())
    return " ".join(printable.split())[:MAX_TITLE_LENGTH]


@dataclass
class Source:
    """A document added to a session, with its processing state and metadata.

    Attributes:
        id: Random 16-character hex ID.
        number: Session-unique number used in citations (``S<number>``).
        kind: Whether the source is an uploaded file or a web address.
        title: Display title.
        status: Processing state.
        created_at: ISO timestamp of when the source was added.
        filename: Sanitized base name of an uploaded file.
        url: Normalized web address as submitted.
        final_url: Web address after redirects.
        document_date: Document date as entered by the user or detected from the content.
        document_date_origin: Where the date came from: ``user``, ``file metadata``,
            ``page metadata`` or ``server``; empty when there is no date.
        retrieved_at: ISO timestamp of when a web address was downloaded.
        format: Detected `SourceFormat` value.
        size_bytes: Size of the original content.
        page_count: Number of pages; 0 for formats without pages.
        pages_done: Conversion progress in pages.
        chunk_count: Number of citable chunks after conversion.
        token_count: Estimated tokens of all chunks.
        error_code: Machine-readable code of the last failure.
        error_message: User-facing message of the last failure.
        limitations: User-facing notes on content that could not be converted.
        version: Processing version; bumped on cancel and retry so that jobs for older
            versions discard their results.
        etag: Table entity ETag for optimistic concurrency; not stored in the entity data.
    """

    id: str
    number: int
    kind: SourceKind
    title: str
    status: SourceStatus
    created_at: str
    filename: str = ""
    url: str = ""
    final_url: str = ""
    document_date: str = ""
    document_date_origin: str = ""
    retrieved_at: str = ""
    format: str = ""
    size_bytes: int = 0
    page_count: int = 0
    pages_done: int = 0
    chunk_count: int = 0
    token_count: int = 0
    error_code: str = ""
    error_message: str = ""
    limitations: list[str] = field(default_factory=list)
    version: int = 1
    etag: str = ""

    @property
    def reference(self) -> str:
        """Citation prefix of the source, e.g. ``S3``."""
        return f"S{self.number}"

    @property
    def is_active(self) -> bool:
        """Whether the source is queued, fetching or converting."""
        return self.status in ACTIVE_STATUSES

    @property
    def can_retry(self) -> bool:
        """Whether the source failed or was cancelled and can be queued again."""
        return self.status in RETRYABLE_STATUSES

    def to_data(self) -> dict[str, Any]:
        """Return the table entity data; the ID and ETag are stored by the table itself."""
        return {
            "number": self.number,
            "kind": self.kind.value,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at,
            "filename": self.filename,
            "url": self.url,
            "final_url": self.final_url,
            "document_date": self.document_date,
            "document_date_origin": self.document_date_origin,
            "retrieved_at": self.retrieved_at,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "page_count": self.page_count,
            "pages_done": self.pages_done,
            "chunk_count": self.chunk_count,
            "token_count": self.token_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "limitations": json.dumps(self.limitations),
            "version": self.version,
        }

    @classmethod
    def from_stored(cls, source_id: str, stored: StoredEntity) -> "Source":
        """Build a source from its ID and stored table entity."""
        data = stored.data
        return cls(
            id=source_id,
            number=int(data["number"]),
            kind=SourceKind(data["kind"]),
            title=data.get("title", ""),
            status=SourceStatus(data["status"]),
            created_at=data.get("created_at", ""),
            filename=data.get("filename", ""),
            url=data.get("url", ""),
            final_url=data.get("final_url", ""),
            document_date=data.get("document_date", ""),
            document_date_origin=data.get("document_date_origin", ""),
            retrieved_at=data.get("retrieved_at", ""),
            format=data.get("format", ""),
            size_bytes=int(data.get("size_bytes", 0)),
            page_count=int(data.get("page_count", 0)),
            pages_done=int(data.get("pages_done", 0)),
            chunk_count=int(data.get("chunk_count", 0)),
            token_count=int(data.get("token_count", 0)),
            error_code=data.get("error_code", ""),
            error_message=data.get("error_message", ""),
            limitations=json.loads(data.get("limitations") or "[]"),
            version=int(data.get("version", 1)),
            etag=stored.etag,
        )


@dataclass(frozen=True)
class SourceSummary:
    """Counts and token totals over all sources of a session.

    Attributes:
        total: Number of sources.
        active: Sources that are still queued, fetching or converting.
        converted: Sources that converted successfully.
        failed: Sources that failed or were cancelled.
        tokens: Estimated tokens of the converted sources.
        token_limit: Maximum tokens allowed for all converted sources together.
    """

    total: int
    active: int
    converted: int
    failed: int
    tokens: int
    token_limit: int

    @property
    def over_token_limit(self) -> bool:
        """Whether the converted sources together exceed the token limit."""
        return self.tokens > self.token_limit

    @property
    def ready(self) -> bool:
        """Whether nothing is processing, something converted and the token limit is met."""
        return self.active == 0 and self.converted > 0 and not self.over_token_limit


class SourceRepository:
    """Source entities stored in the session's table partition.

    Args:
        table: Table store holding sessions and their sources.
    """

    def __init__(self, table: TableStore) -> None:
        self._table = table

    def list_for(self, session_key: str) -> list[Source]:
        """Return all sources of a session, ordered by number."""
        sources = [
            Source.from_stored(row.removeprefix(SOURCE_ROW_PREFIX), entity)
            for row, entity in self._table.list_partition(session_key)
            if row.startswith(SOURCE_ROW_PREFIX)
        ]
        return sorted(sources, key=lambda source: source.number)

    def get(self, session_key: str, source_id: str) -> Source | None:
        """Return a source, or None if it does not exist."""
        stored = self._table.get(session_key, source_row(source_id))
        return Source.from_stored(source_id, stored) if stored else None

    def insert(self, session_key: str, source: Source) -> None:
        """Insert a new source entity and set its ETag."""
        source.etag = self._table.insert(session_key, source_row(source.id), source.to_data())

    def delete(self, session_key: str, source_id: str) -> None:
        """Delete a source entity; its blobs are left untouched."""
        self._table.delete(session_key, source_row(source_id))

    def mutate(
        self, session_key: str, source_id: str, change: Callable[[Source], bool]
    ) -> Source | None:
        """Apply a change to a source with optimistic concurrency, retrying on conflicts.

        Args:
            session_key: Key of the session that owns the source.
            source_id: ID of the source to change.
            change: Mutates the freshly read source in place and returns False to skip the
                write. It may be called several times.

        Returns:
            The written source, the unchanged source if `change` skipped the write, or None
            if the source does not exist (anymore).

        Raises:
            ConflictError: If the source kept changing concurrently.
        """
        for _ in range(_MAX_UPDATE_ATTEMPTS):
            source = self.get(session_key, source_id)
            if source is None:
                return None
            if not change(source):
                return source
            try:
                source.etag = self._table.update(
                    session_key, source_row(source_id), source.to_data(), source.etag
                )
            except ConflictError:
                continue
            except NotFoundError:
                return None
            return source
        raise ConflictError("Source is busy; try again")


class SourceService:
    """Add, update, cancel, retry, remove and approve the sources of a session.

    Args:
        table: Table store holding sessions and their sources.
        blobs: Blob store for original files, markdown and chunks.
        queue: Queue for background fetch and conversion jobs.
        sessions: Session service used to allocate numbers and bump versions.
        settings: Application settings with source limits.
        clock: Returns the current UTC time.
    """

    def __init__(
        self,
        table: TableStore,
        blobs: BlobStore,
        queue: JobQueue,
        sessions: SessionService,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.repository = SourceRepository(table)
        self._blobs = blobs
        self._queue = queue
        self._sessions = sessions
        self._settings = settings
        self._clock = clock

    def list_for(self, session_key: str) -> list[Source]:
        """Return all sources of a session, ordered by number."""
        return self.repository.list_for(session_key)

    def get(self, session_key: str, source_id: str) -> Source | None:
        """Return a source, or None if it does not exist."""
        return self.repository.get(session_key, source_id)

    def summary(self, session_key: str) -> SourceSummary:
        """Summarize the processing state and token use of a session's sources."""
        sources = self.list_for(session_key)
        converted = [source for source in sources if source.status == SourceStatus.CONVERTED]
        return SourceSummary(
            total=len(sources),
            active=sum(source.is_active for source in sources),
            converted=len(converted),
            failed=sum(source.can_retry for source in sources),
            tokens=sum(source.token_count for source in converted),
            token_limit=self._settings.max_session_source_tokens,
        )

    def add_file(
        self,
        session: WorkspaceSession,
        *,
        filename: str,
        data: bytes,
        title: str = "",
        document_date: str = "",
    ) -> Source:
        """Validate and store an uploaded file, then queue it for conversion.

        The format is detected from the content. Without a user title, the title comes from
        the file metadata or the file name; without a user date, the date comes from the file
        metadata.

        Args:
            session: Session to add the source to.
            filename: Original file name; only its sanitized base name is kept.
            data: File content.
            title: Optional title entered by the user.
            document_date: Optional document date entered by the user.

        Returns:
            The new, queued source.

        Raises:
            SourceError: ``too_many_sources``, ``too_large``, ``session_too_large`` or
                ``session_too_many_pages`` when a limit is exceeded, an inspection code (see
                `inspect_content`) for rejected content, or ``queue_unavailable`` if the job
                could not be queued (the source is then stored as failed).
        """
        settings = self._settings
        existing = self.list_for(session.key)
        self._check_source_count(existing)
        if len(data) > settings.max_file_bytes:
            raise SourceError(
                "too_large",
                f"Files can be at most {settings.max_file_bytes // (1024 * 1024)} MB.",
            )
        if sum(source.size_bytes for source in existing) + len(data) > settings.max_session_bytes:
            raise SourceError(
                "session_too_large", "Adding this file would exceed the session's storage limit."
            )
        inspected = inspect_content(data, max_pages=settings.max_document_pages)
        if (
            sum(source.page_count for source in existing) + inspected.page_count
            > settings.max_session_pages
        ):
            raise SourceError(
                "session_too_many_pages",
                f"Adding this file would exceed the limit of {settings.max_session_pages} pages.",
            )

        safe_filename = clean_title(filename.replace("\\", "/").rsplit("/", 1)[-1])
        source = self._new_source(
            session,
            kind=SourceKind.FILE,
            title=clean_title(title) or inspected.title or safe_filename or "Untitled file",
            filename=safe_filename,
        )
        source.format = inspected.format.value
        source.size_bytes = len(data)
        source.page_count = inspected.page_count
        _set_date(source, document_date, "user")
        if not source.document_date:
            _set_date(source, inspected.document_date, "file metadata")
        self._blobs.put(original_blob(session.key, source.id), data)
        self.repository.insert(session.key, source)
        self._enqueue(session.key, source)
        return source

    def add_url(
        self, session: WorkspaceSession, *, url: str, title: str = "", document_date: str = ""
    ) -> Source:
        """Validate a web address and queue it to be downloaded and converted.

        Only the address itself is checked here; DNS, download size and page limits are
        checked by the background job.

        Args:
            session: Session to add the source to.
            url: Web address entered by the user.
            title: Optional title entered by the user; otherwise taken from the download.
            document_date: Optional document date entered by the user.

        Returns:
            The new, queued source.

        Raises:
            SourceError: ``invalid_url`` or ``blocked_address`` for a disallowed address,
                ``too_many_sources``, or ``queue_unavailable`` if the job could not be queued
                (the source is then stored as failed).
        """
        target = parse_url(url)
        self._check_source_count(self.list_for(session.key))
        source = self._new_source(
            session, kind=SourceKind.URL, title=clean_title(title), filename=""
        )
        source.url = target.url
        _set_date(source, document_date, "user")
        self.repository.insert(session.key, source)
        self._enqueue(session.key, source)
        return source

    def update_metadata(
        self, session_key: str, source_id: str, *, title: str, document_date: str
    ) -> Source | None:
        """Update the title and user-entered document date of a source.

        Args:
            session_key: Key of the session that owns the source.
            source_id: ID of the source to update.
            title: New title; an empty title keeps the current one.
            document_date: New document date; an empty value clears a user-entered date but
                keeps a date found in metadata.

        Returns:
            The updated source, or None if it does not exist.

        Raises:
            ConflictError: If the source kept changing concurrently.
        """

        def apply(source: Source) -> bool:
            source.title = clean_title(title) or source.title
            if document_date:
                _set_date(source, document_date, "user")
            elif source.document_date_origin == "user":
                source.document_date = ""
                source.document_date_origin = ""
            return True

        updated = self.repository.mutate(session_key, source_id, apply)
        if updated is not None:
            self._sessions.bump_sources_version(session_key)
        return updated

    def cancel(self, session_key: str, source_id: str) -> Source | None:
        """Cancel a queued or running source.

        The version is bumped so that a running job discards its results.

        Returns:
            The source (unchanged if it was not active), or None if it does not exist.

        Raises:
            ConflictError: If the source kept changing concurrently.
        """

        def apply(source: Source) -> bool:
            if not source.is_active:
                return False
            source.status = SourceStatus.CANCELLED
            source.version += 1
            source.error_code = "cancelled"
            source.error_message = "Processing was cancelled."
            return True

        cancelled = self.repository.mutate(session_key, source_id, apply)
        if cancelled is not None:
            self._sessions.bump_sources_version(session_key)
        return cancelled

    def retry(self, session_key: str, source_id: str) -> Source | None:
        """Queue a failed or cancelled source again under a new version.

        Returns:
            The source (unchanged if it could not be retried), or None if it does not exist.

        Raises:
            ConflictError: If the source kept changing concurrently.
            SourceError: ``queue_unavailable`` if the job could not be queued (the source is
                then stored as failed).
        """

        def apply(source: Source) -> bool:
            if not source.can_retry:
                return False
            source.status = SourceStatus.QUEUED
            source.version += 1
            source.pages_done = 0
            source.error_code = ""
            source.error_message = ""
            source.limitations = []
            return True

        source = self.repository.mutate(session_key, source_id, apply)
        if source is not None and source.status == SourceStatus.QUEUED:
            self._sessions.bump_sources_version(session_key)
            self._enqueue(session_key, source)
        return source

    def remove(self, session_key: str, source_id: str) -> bool:
        """Delete a source with its original file, markdown and chunks.

        Returns:
            False if the source did not exist, otherwise True.
        """
        if self.get(session_key, source_id) is None:
            return False
        # Deleting the entity first makes any running job discard its results.
        self.repository.delete(session_key, source_id)
        self._blobs.delete_prefix(source_blob_prefix(session_key, source_id))
        self._sessions.bump_sources_version(session_key)
        return True

    def approve(self, session_key: str, *, accept_failed: bool) -> WorkspaceSession:
        """Approve the current set of sources so that drafting can start.

        Args:
            session_key: Key of the session to approve.
            accept_failed: Continue even though some sources failed or were cancelled.

        Returns:
            The session with its approved sources version updated.

        Raises:
            SourceError: ``not_ready`` if sources are still processing or none converted,
                ``too_many_tokens`` if the converted sources exceed the token limit, or
                ``failed_sources`` if some failed and `accept_failed` is False.
            NotFoundError: If the session no longer exists or has expired.
        """
        summary = self.summary(session_key)
        if summary.active:
            raise SourceError("not_ready", "Wait until all sources have finished processing.")
        if not summary.converted:
            raise SourceError("not_ready", "Add at least one source that converts successfully.")
        if summary.over_token_limit:
            raise SourceError(
                "too_many_tokens",
                "The converted sources are too long to process together. Remove some sources.",
            )
        if summary.failed and not accept_failed:
            raise SourceError(
                "failed_sources",
                "Some sources failed. Retry or remove them, or confirm that you want to "
                "continue without them.",
            )
        return self._sessions.approve_sources(session_key)

    def _check_source_count(self, existing: list[Source]) -> None:
        if len(existing) >= self._settings.max_sources:
            raise SourceError(
                "too_many_sources",
                f"A session can have at most {self._settings.max_sources} sources.",
            )

    def _new_source(
        self, session: WorkspaceSession, *, kind: SourceKind, title: str, filename: str
    ) -> Source:
        return Source(
            id=secrets.token_hex(8),
            number=self._sessions.allocate_source_number(session.key),
            kind=kind,
            title=title,
            filename=filename,
            status=SourceStatus.QUEUED,
            created_at=self._clock().isoformat(),
        )

    def _enqueue(self, session_key: str, source: Source) -> None:
        try:
            self._queue.send(
                {"session": session_key, "source": source.id, "version": source.version}
            )
        except Exception as error:

            def mark_failed(current: Source) -> bool:
                if current.version != source.version:
                    return False
                current.status = SourceStatus.FAILED
                current.error_code = "queue_unavailable"
                current.error_message = "Processing could not be started. Try again."
                return True

            self.repository.mutate(session_key, source.id, mark_failed)
            raise SourceError(
                "queue_unavailable", "Processing could not be started. Try again."
            ) from error


def _set_date(source: Source, value: str, origin: str) -> None:
    if value:
        source.document_date = value
        source.document_date_origin = origin
