"""Workflow state for structure proposal/review and content generation.

Structure versions are immutable blobs; the `structure` row points at the current draft and
the approved version. Content results are stored per sub-category and record the structure,
sources and context versions they were generated from, so stale results are detectable.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, cast

from .content import GeneratedContent
from .model import model_configured
from .sessions import WorkspaceSession, session_blob_prefix, utcnow
from .settings import Settings
from .storage import BlobStore, JobQueue, NotFoundError, TableStore, mutate_entity
from .structure import Structure, StructureError

STRUCTURE_ROW = "structure"
CONTENT_ROW_PREFIX = "content:"
ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


class DraftingError(Exception):
    """A drafting action that cannot be performed in the current state.

    Args:
        message: User-facing explanation, safe to show in the UI.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _StaleJobError(Exception):
    """The job was superseded; its results must be discarded."""


def new_job_id() -> str:
    """Return a new random identifier for a job or blob."""
    return secrets.token_hex(8)


def structure_blob(session_key: str, blob_id: str) -> str:
    """Return the blob name of a structure version."""
    return f"{session_blob_prefix(session_key)}structure/{blob_id}.json"


def content_blob(session_key: str, blob_id: str) -> str:
    """Return the blob name of a content version."""
    return f"{session_blob_prefix(session_key)}content/{blob_id}.json"


def content_row(subcategory_key: str) -> str:
    """Return the table row key of a sub-category's content state."""
    return f"{CONTENT_ROW_PREFIX}{subcategory_key}"


def _from_data[T](cls: type[T], data: dict[str, Any]) -> T:
    names = {item.name for item in fields(cast(Any, cls))}
    return cls(**{key: value for key, value in data.items() if key in names})


@dataclass
class StructureState:
    """The structure workflow row of a session.

    Attributes:
        version: Draft version, incremented on every proposal and edit; used for optimistic
            concurrency.
        blob: Blob id of the current draft, or empty if there is none.
        approved_version: Draft version that was last approved, or 0.
        approved_blob: Blob id of the last approved draft, or empty.
        sources_version: Approved sources version the last proposal was based on.
        context_version: Context version the last proposal was based on.
        job: Id of the latest proposal job; results of other jobs are discarded.
        job_status: ``queued``, ``running``, ``failed`` or empty when idle.
        job_error: User-facing error of a failed job.
        instructions: Staff instructions for the latest proposal job.
        notes: The model's notes on the latest proposal.
    """

    version: int = 0
    blob: str = ""
    approved_version: int = 0
    approved_blob: str = ""
    sources_version: int = -1
    context_version: int = -1
    job: str = ""
    job_status: str = ""
    job_error: str = ""
    instructions: str = ""
    notes: str = ""

    @property
    def has_draft(self) -> bool:
        """Whether a draft structure exists."""
        return bool(self.blob)

    @property
    def approved(self) -> bool:
        """Whether the current draft version is the approved one."""
        return bool(self.approved_blob) and self.approved_version == self.version

    @property
    def job_active(self) -> bool:
        """Whether a proposal job is queued or running."""
        return self.job_status in ACTIVE_JOB_STATUSES


@dataclass
class ContentState:
    """The content workflow row of one sub-category.

    Attributes:
        subcategory: Key of the sub-category.
        status: ``queued``, ``running``, ``done``, ``failed`` or empty.
        job: Id of the latest generation job; results of other jobs are discarded.
        error: User-facing error of a failed job.
        blob: Blob id of the current content version, or empty.
        structure_version: Approved structure version the content was generated from.
        sources_version: Approved sources version the content was generated from.
        context_version: Context version the content was generated from.
        offers: Number of offers in the current version.
        questions: Number of questions in the current version.
        issues: Number of issues in the current version.
        generated_at: ISO timestamp of the last generation.
        approved_blob: Blob id of the approved content version, or empty.
        empty_decision: Reviewer decision for a sub-category without usable content: ``keep``,
            ``drop`` (leave it out of the HIA) or empty.
        edited: Whether a reviewer edited the generated content.
    """

    subcategory: str
    status: str = ""
    job: str = ""
    error: str = ""
    blob: str = ""
    structure_version: int = 0
    sources_version: int = -1
    context_version: int = -1
    offers: int = 0
    questions: int = 0
    issues: int = 0
    generated_at: str = ""
    approved_blob: str = ""
    empty_decision: str = ""
    edited: bool = False

    @property
    def job_active(self) -> bool:
        """Whether a generation job is queued or running."""
        return self.status in ACTIVE_JOB_STATUSES

    @property
    def approved(self) -> bool:
        """Whether the current content version is the approved one."""
        return bool(self.blob) and self.approved_blob == self.blob

    def is_current(self, structure: StructureState, session: WorkspaceSession) -> bool:
        """Return whether the content was generated from the current approved inputs.

        Args:
            structure: Current structure state of the session.
            session: Current workspace session.

        Returns:
            False if the approved structure, approved sources or context changed since the
            content was generated.
        """
        return (
            self.structure_version == structure.approved_version
            and self.sources_version == session.approved_sources_version
            and self.context_version == session.context_version
        )


class StructureService:
    """Structure proposal, editing and approval for a session.

    Every change is checked against the expected draft version (optimistic concurrency),
    and each saved draft is a new immutable blob.

    Args:
        table: Table store holding the structure row.
        blobs: Blob store holding structure versions.
        queue: Queue for proposal jobs.
        settings: Application settings.
    """

    def __init__(
        self,
        table: TableStore,
        blobs: BlobStore,
        queue: JobQueue,
        settings: Settings,
    ) -> None:
        self._table = table
        self._blobs = blobs
        self._queue = queue
        self._settings = settings

    def state(self, session_key: str) -> StructureState:
        """Return the structure state of a session, or an empty state if there is none."""
        stored = self._table.get(session_key, STRUCTURE_ROW)
        return _from_data(StructureState, stored.data) if stored else StructureState()

    def load(self, session_key: str, blob_id: str) -> Structure:
        """Load a stored structure version."""
        return Structure.from_json(self._blobs.get(structure_blob(session_key, blob_id)))

    def draft(self, session_key: str) -> tuple[StructureState, Structure | None]:
        """Return the structure state and the current draft, or None if there is no draft."""
        state = self.state(session_key)
        return state, self.load(session_key, state.blob) if state.blob else None

    def approved(self, session_key: str) -> tuple[StructureState, Structure | None]:
        """Return the structure state and the last approved structure, or None.

        The approved structure may be older than the draft; check `StructureState.approved`
        to know whether the draft is still approved.
        """
        state = self.state(session_key)
        if not state.approved_blob:
            return state, None
        return state, self.load(session_key, state.approved_blob)

    def request_proposal(self, session: WorkspaceSession, instructions: str = "") -> None:
        """Queue a job that proposes a new structure or revises the current draft.

        Args:
            session: Workspace session with approved sources and context.
            instructions: Optional staff instructions; with an existing draft, the model
                revises that draft instead of starting over.

        Raises:
            DraftingError: If the model is not configured, the sources are not approved, a
                proposal is already in progress, or the job could not be queued.
        """
        if not model_configured(self._settings):
            raise DraftingError("The AI model is not configured.")
        if session.context is None or not session.sources_approved:
            raise DraftingError("Approve the sources before proposing a structure.")
        job = new_job_id()

        def start(data: dict[str, Any]) -> bool:
            if data.get("job_status") in ACTIVE_JOB_STATUSES:
                raise DraftingError("A structure proposal is already in progress.")
            data.update(job=job, job_status="queued", job_error="", instructions=instructions)
            return True

        mutate_entity(self._table, session.key, STRUCTURE_ROW, start, create=True)
        self._send(session.key, {"session": session.key, "kind": "structure", "job": job})

    def edit(
        self, session_key: str, expected_version: int, change: Callable[[Structure], None]
    ) -> StructureState:
        """Apply an edit to a copy of the draft and save it as a new version.

        Args:
            session_key: Key of the workspace session.
            expected_version: Draft version the user edited.
            change: Edit operation applied in place, e.g. from the `structure` module.

        Returns:
            The updated structure state.

        Raises:
            DraftingError: If the draft changed since ``expected_version``, a proposal is
                running, or the edit is invalid.
        """
        state, structure = self.draft(session_key)
        if structure is None or state.version != expected_version:
            raise DraftingError("The structure changed in the meantime. Review it and try again.")
        if state.job_active:
            raise DraftingError("Wait until the structure proposal has finished.")
        updated = structure.copy()
        try:
            change(updated)
        except StructureError as error:
            raise DraftingError(error.message) from error
        return self._save(session_key, updated, expected_version=expected_version)

    def approve(self, session_key: str, expected_version: int) -> StructureState:
        """Approve the current draft so content can be generated from it.

        Args:
            session_key: Key of the workspace session.
            expected_version: Draft version the user reviewed.

        Returns:
            The updated structure state.

        Raises:
            DraftingError: If the draft changed since ``expected_version``, does not exist,
                or has problems (see `Structure.problems`).
        """
        state, structure = self.draft(session_key)
        if structure is None or state.version != expected_version:
            raise DraftingError("The structure changed in the meantime. Review it and try again.")
        problems = structure.problems()
        if problems:
            raise DraftingError(problems[0])

        def apply(data: dict[str, Any]) -> bool:
            if data.get("version") != expected_version:
                raise DraftingError("The structure changed in the meantime. Try again.")
            data.update(approved_version=data["version"], approved_blob=data["blob"])
            return True

        updated = mutate_entity(self._table, session_key, STRUCTURE_ROW, apply)
        if updated is None:
            raise DraftingError("There is no structure to approve.")
        return _from_data(StructureState, updated)

    def mark_running(self, session_key: str, job: str) -> bool:
        """Mark a queued proposal job as running.

        Returns:
            False if the job is stale (superseded or no longer queued) and must be skipped.
        """

        def run(data: dict[str, Any]) -> bool:
            if data.get("job") != job or data.get("job_status") != "queued":
                raise _StaleJobError
            data["job_status"] = "running"
            return True

        try:
            return mutate_entity(self._table, session_key, STRUCTURE_ROW, run) is not None
        except _StaleJobError:
            return False

    def complete_proposal(
        self,
        session_key: str,
        job: str,
        structure: Structure,
        *,
        sources_version: int,
        context_version: int,
        notes: str,
    ) -> bool:
        """Store a proposed structure as the new draft version, if the job is still current.

        Args:
            session_key: Key of the workspace session.
            job: Id of the proposal job.
            structure: Proposed structure.
            sources_version: Approved sources version the proposal was based on.
            context_version: Context version the proposal was based on.
            notes: The model's notes for staff.

        Returns:
            False if the job is stale and the proposal was discarded.
        """
        blob_id = new_job_id()
        self._blobs.put(structure_blob(session_key, blob_id), structure.to_json().encode("utf-8"))

        def commit(data: dict[str, Any]) -> bool:
            if data.get("job") != job or data.get("job_status") != "running":
                raise _StaleJobError
            data.update(
                version=int(data.get("version", 0)) + 1,
                blob=blob_id,
                sources_version=sources_version,
                context_version=context_version,
                job_status="",
                job_error="",
                notes=notes,
            )
            return True

        try:
            return mutate_entity(self._table, session_key, STRUCTURE_ROW, commit) is not None
        except _StaleJobError:
            return False

    def fail_job(self, session_key: str, job: str, message: str) -> None:
        """Mark an active proposal job as failed; stale jobs are ignored.

        Args:
            session_key: Key of the workspace session.
            job: Id of the proposal job.
            message: User-facing error message.
        """

        def fail(data: dict[str, Any]) -> bool:
            if data.get("job") != job or data.get("job_status") not in ACTIVE_JOB_STATUSES:
                return False
            data.update(job_status="failed", job_error=message)
            return True

        mutate_entity(self._table, session_key, STRUCTURE_ROW, fail)

    def _save(
        self, session_key: str, structure: Structure, *, expected_version: int
    ) -> StructureState:
        blob_id = new_job_id()
        self._blobs.put(structure_blob(session_key, blob_id), structure.to_json().encode("utf-8"))

        def save(data: dict[str, Any]) -> bool:
            if data.get("version") != expected_version or data.get("job_status") in (
                ACTIVE_JOB_STATUSES
            ):
                raise DraftingError("The structure changed in the meantime. Try again.")
            data.update(version=expected_version + 1, blob=blob_id)
            return True

        updated = mutate_entity(self._table, session_key, STRUCTURE_ROW, save)
        if updated is None:
            raise DraftingError("There is no structure to edit.")
        return _from_data(StructureState, updated)

    def _send(self, session_key: str, payload: dict[str, Any]) -> None:
        try:
            self._queue.send(payload)
        except Exception as error:
            self.fail_job(session_key, payload["job"], "Processing could not be started.")
            raise DraftingError("Processing could not be started. Try again.") from error


class ContentService:
    """Content generation, editing and review per sub-category of the approved structure.

    Each content row records the structure, sources and context versions it was generated
    from, so outdated content can be detected and regenerated.

    Args:
        table: Table store holding the content rows.
        blobs: Blob store holding content versions.
        queue: Queue for generation jobs.
        structures: Structure service, used to read the approved structure.
        settings: Application settings.
        clock: Returns the current time, used for generation and edit dates.
    """

    def __init__(
        self,
        table: TableStore,
        blobs: BlobStore,
        queue: JobQueue,
        structures: StructureService,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._table = table
        self._blobs = blobs
        self._queue = queue
        self._structures = structures
        self._settings = settings
        self._clock = clock

    def states(self, session_key: str) -> dict[str, ContentState]:
        """Return the content states of a session, keyed by sub-category key."""
        return {
            row.removeprefix(CONTENT_ROW_PREFIX): _from_data(ContentState, entity.data)
            for row, entity in self._table.list_partition(session_key)
            if row.startswith(CONTENT_ROW_PREFIX)
        }

    def state(self, session_key: str, subcategory_key: str) -> ContentState | None:
        """Return the content state of a sub-category, or None if nothing was requested."""
        stored = self._table.get(session_key, content_row(subcategory_key))
        return _from_data(ContentState, stored.data) if stored else None

    def result(self, session_key: str, state: ContentState) -> GeneratedContent | None:
        """Load the current content version of a state, or None if there is none."""
        if not state.blob:
            return None
        try:
            raw = self._blobs.get(content_blob(session_key, state.blob))
        except NotFoundError:
            return None
        return GeneratedContent.model_validate_json(raw)

    def request(self, session: WorkspaceSession, subcategory_keys: list[str] | None = None) -> int:
        """Queue content generation for sub-categories of the approved structure.

        Sub-categories with an active job are skipped. Jobs are queued in the structure's
        order, so a single worker generates them top to bottom as shown on the content page.

        Args:
            session: Workspace session with approved sources.
            subcategory_keys: Sub-categories to (re)generate; keys not in the approved
                structure are ignored. If None, all sub-categories that are missing, failed
                or outdated are queued.

        Returns:
            The number of jobs queued.

        Raises:
            DraftingError: If the model is not configured, the sources or structure are not
                approved, or a job could not be queued.
        """
        if not model_configured(self._settings):
            raise DraftingError("The AI model is not configured.")
        if not session.sources_approved:
            raise DraftingError("Approve the sources before generating content.")
        structure_state, structure = self._structures.approved(session.key)
        if structure is None or not structure_state.approved:
            raise DraftingError("Approve the structure before generating content.")
        keys = [sub.key for _category, sub in structure.subcategories()]
        states = self.states(session.key)
        if subcategory_keys is None:
            targets = [
                key
                for key in keys
                if key not in states
                or states[key].status == "failed"
                or (
                    states[key].status == "done"
                    and not states[key].is_current(structure_state, session)
                )
            ]
        else:
            targets = [key for key in keys if key in subcategory_keys]
        queued = 0
        for key in targets:
            if key in states and states[key].job_active:
                continue
            job = new_job_id()

            def start(data: dict[str, Any], key: str = key, job: str = job) -> bool:
                if data.get("status") in ACTIVE_JOB_STATUSES:
                    return False
                data.update(subcategory=key, status="queued", job=job, error="")
                return True

            mutate_entity(self._table, session.key, content_row(key), start, create=True)
            try:
                self._queue.send(
                    {"session": session.key, "kind": "content", "node": key, "job": job}
                )
            except Exception as error:
                self.fail_job(session.key, key, job, "Processing could not be started.")
                raise DraftingError("Processing could not be started. Try again.") from error
            queued += 1
        return queued

    def mark_running(self, session_key: str, subcategory_key: str, job: str) -> bool:
        """Mark a queued generation job as running.

        Returns:
            False if the job is stale (superseded or no longer queued) and must be skipped.
        """

        def run(data: dict[str, Any]) -> bool:
            if data.get("job") != job or data.get("status") != "queued":
                raise _StaleJobError
            data["status"] = "running"
            return True

        try:
            return (
                mutate_entity(self._table, session_key, content_row(subcategory_key), run)
                is not None
            )
        except _StaleJobError:
            return False

    def complete(
        self,
        session_key: str,
        subcategory_key: str,
        job: str,
        content: GeneratedContent,
        *,
        structure_version: int,
        sources_version: int,
        context_version: int,
    ) -> bool:
        """Store generated content as the new version, if the job is still current.

        A new version resets approval, reviewer decisions and the edited flag. The previous
        version's blob is deleted; on a stale job the new blob is deleted instead.

        Args:
            session_key: Key of the workspace session.
            subcategory_key: Key of the sub-category.
            job: Id of the generation job.
            content: Checked generated content.
            structure_version: Approved structure version the content was generated from.
            sources_version: Approved sources version the content was generated from.
            context_version: Context version the content was generated from.

        Returns:
            False if the job is stale and the content was discarded.
        """
        blob_id = new_job_id()
        self._blobs.put(content_blob(session_key, blob_id), content.model_dump_json().encode())
        previous = self.state(session_key, subcategory_key)

        def commit(data: dict[str, Any]) -> bool:
            if data.get("job") != job or data.get("status") != "running":
                raise _StaleJobError
            data.update(
                status="done",
                error="",
                blob=blob_id,
                structure_version=structure_version,
                sources_version=sources_version,
                context_version=context_version,
                offers=len(content.offers),
                questions=len(content.questions),
                issues=len(content.issues),
                generated_at=self._clock().isoformat(),
                approved_blob="",
                empty_decision="",
                edited=False,
            )
            return True

        try:
            committed = (
                mutate_entity(self._table, session_key, content_row(subcategory_key), commit)
                is not None
            )
        except _StaleJobError:
            committed = False
        if not committed:
            self._blobs.delete_prefix(content_blob(session_key, blob_id))
        elif previous and previous.blob:
            self._blobs.delete_prefix(content_blob(session_key, previous.blob))
        return committed

    def fail_job(self, session_key: str, subcategory_key: str, job: str, message: str) -> None:
        """Mark an active generation job as failed; stale jobs are ignored.

        Args:
            session_key: Key of the workspace session.
            subcategory_key: Key of the sub-category.
            job: Id of the generation job.
            message: User-facing error message.
        """

        def fail(data: dict[str, Any]) -> bool:
            if data.get("job") != job or data.get("status") not in ACTIVE_JOB_STATUSES:
                return False
            data.update(status="failed", error=message)
            return True

        mutate_entity(self._table, session_key, content_row(subcategory_key), fail)

    def save_edit(
        self,
        session_key: str,
        subcategory_key: str,
        expected_blob: str,
        content: GeneratedContent,
    ) -> ContentState:
        """Store reviewer edits as a new version; approval must then be given again.

        Args:
            session_key: Key of the workspace session.
            subcategory_key: Key of the sub-category.
            expected_blob: Content version the reviewer edited.
            content: Edited content; its ``updated_on`` date is set to today.

        Returns:
            The updated content state.

        Raises:
            DraftingError: If the content changed since ``expected_blob``, a job is active,
                or the content no longer exists.
        """
        content.updated_on = self._clock().date().isoformat()
        blob_id = new_job_id()
        self._blobs.put(content_blob(session_key, blob_id), content.model_dump_json().encode())

        def commit(data: dict[str, Any]) -> bool:
            if data.get("blob") != expected_blob or data.get("status") in ACTIVE_JOB_STATUSES:
                raise DraftingError("This content changed in the meantime. Reload and try again.")
            data.update(
                blob=blob_id,
                offers=len(content.offers),
                questions=len(content.questions),
                issues=len(content.issues),
                edited=True,
            )
            return True

        try:
            updated = mutate_entity(self._table, session_key, content_row(subcategory_key), commit)
        except DraftingError:
            self._blobs.delete_prefix(content_blob(session_key, blob_id))
            raise
        if updated is None:
            raise DraftingError("This content no longer exists.")
        self._blobs.delete_prefix(content_blob(session_key, expected_blob))
        return _from_data(ContentState, updated)

    def review(
        self,
        session_key: str,
        subcategory_key: str,
        expected_blob: str,
        *,
        approve: bool,
        empty_decision: str = "",
    ) -> ContentState:
        """Approve or withdraw approval of a content version.

        Args:
            session_key: Key of the workspace session.
            subcategory_key: Key of the sub-category.
            expected_blob: Content version the reviewer reviewed.
            approve: True to approve the version, False to withdraw approval.
            empty_decision: For a sub-category without usable content, ``keep`` or ``drop``.

        Returns:
            The updated content state.

        Raises:
            DraftingError: If the content changed since ``expected_blob``, is not done, or no
                longer exists.
        """

        def apply(data: dict[str, Any]) -> bool:
            if data.get("blob") != expected_blob or data.get("status") != "done":
                raise DraftingError("This content changed in the meantime. Reload and try again.")
            data.update(
                approved_blob=expected_blob if approve else "",
                empty_decision=empty_decision,
            )
            return True

        updated = mutate_entity(self._table, session_key, content_row(subcategory_key), apply)
        if updated is None:
            raise DraftingError("This content no longer exists.")
        return _from_data(ContentState, updated)
