"""Background processing of structure proposals and per-sub-category content generation."""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .checks import ChunkIndex, ChunkRef, check_draft
from .chunking import estimate_tokens
from .content import StructureProposal, SubcategoryDraft
from .context import ProjectContext
from .drafting import ContentService, StructureService
from .inspection import format_label
from .model import ModelClient, ModelError
from .prompts import (
    PROMPT_VERSION,
    SourceChunk,
    SourceMaterial,
    content_messages,
    structure_messages,
)
from .sessions import SessionService, WorkspaceSession, utcnow
from .settings import Settings
from .sources import SourceRepository, SourceStatus, chunks_blob
from .storage import BlobStore, JobQueue, NotFoundError, QueueMessage, TableStore
from .structure import (
    MAX_CATEGORIES,
    MAX_SUBCATEGORIES,
    Node,
    Structure,
    StructureError,
    clean_description,
    clean_name,
    new_key,
)

logger = logging.getLogger(__name__)


class JobFailedError(Exception):
    """A generation job failed for a reason the user can act on.

    Args:
        message: User-facing explanation, stored on the job and shown in the UI.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def load_source_material(
    sources: SourceRepository, blobs: BlobStore, session_key: str
) -> tuple[list[SourceMaterial], ChunkIndex]:
    """Load the passages of all converted sources in a session.

    Sources that are not converted or whose chunks are missing are skipped.

    Args:
        sources: Repository of the session's sources.
        blobs: Blob store holding the chunk files.
        session_key: Key of the workspace session.

    Returns:
        The sources as prompt material, and an index of all their passages for checking
        citations.
    """
    materials: list[SourceMaterial] = []
    refs: list[ChunkRef] = []
    for source in sources.list_for(session_key):
        if source.status != SourceStatus.CONVERTED:
            continue
        try:
            chunks = json.loads(blobs.get(chunks_blob(session_key, source.id)))
        except NotFoundError:
            continue
        date = source.document_date
        if date and source.document_date_origin:
            date = f"{date} (from {source.document_date_origin})"
        materials.append(
            SourceMaterial(
                reference=source.reference,
                title=source.title,
                format_label=format_label(source.format) or "Document",
                date=date,
                url=source.final_url or source.url,
                chunks=[SourceChunk(chunk_id=c["id"], text=c["text"]) for c in chunks],
            )
        )
        refs += [
            ChunkRef(
                chunk_id=chunk["id"],
                source_id=source.id,
                source_ref=source.reference,
                source_title=source.title,
                page=chunk.get("page"),
                sheet=chunk.get("sheet", ""),
                text=chunk["text"],
            )
            for chunk in chunks
        ]
    return materials, ChunkIndex(refs)


def proposal_to_structure(proposal: StructureProposal, index: ChunkIndex) -> Structure:
    """Convert a model proposal into a structure with fresh keys.

    Names and descriptions are cleaned, nodes without a name are dropped, counts are capped
    at the structure limits and passage labels unknown to the index are discarded.

    Args:
        proposal: Structure proposed by the model.
        index: Passages of the session's sources.

    Returns:
        The cleaned structure; it may have no categories.
    """
    structure = Structure()
    for category in proposal.categories[:MAX_CATEGORIES]:
        name = clean_name(category.name)
        if not name:
            continue
        node = Node(key=new_key(), name=name, description=clean_description(category.description))
        for sub in category.subcategories[:MAX_SUBCATEGORIES]:
            sub_name = clean_name(sub.name)
            if sub_name:
                node.children.append(
                    Node(
                        key=new_key(),
                        name=sub_name,
                        description=clean_description(sub.description),
                        chunk_ids=index.valid(sub.chunk_ids),
                    )
                )
        structure.categories.append(node)
    return structure


class GenerationProcessor:
    """Process queued structure proposal and content generation jobs.

    Jobs are skipped when their session has expired or they were superseded, failed after
    too many delivery attempts, and refused when the session's model token budget would be
    exceeded. Results are discarded if the sources, context or approved structure changed
    while the job was running.

    Args:
        table: Table store with session, source and workflow state.
        blobs: Blob store with source chunks, structures and content.
        queue: Queue the jobs are received from.
        sessions: Session service, used to check sessions and record token usage.
        structures: Structure workflow service.
        contents: Content workflow service.
        settings: Application settings with job and model limits.
        model_factory: Creates the model client on first use.
        clock: Returns the current time, used for generation dates.
    """

    def __init__(
        self,
        *,
        table: TableStore,
        blobs: BlobStore,
        queue: JobQueue,
        sessions: SessionService,
        structures: StructureService,
        contents: ContentService,
        settings: Settings,
        model_factory: Callable[[], ModelClient],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sources = SourceRepository(table)
        self._blobs = blobs
        self._queue = queue
        self._sessions = sessions
        self._structures = structures
        self._contents = contents
        self._settings = settings
        self._model_factory = model_factory
        self._model: ModelClient | None = None
        self._clock = clock

    def process_next(self) -> bool:
        """Receive and process one job; the message is deleted afterwards, even on failure.

        Returns:
            True if a message was received, False if the queue was empty.
        """
        message = self._queue.receive(self._settings.job_visibility_seconds)
        if message is None:
            return False
        try:
            self._handle(message)
        finally:
            self._queue.delete(message)
        return True

    def _handle(self, message: QueueMessage) -> None:
        payload = message.payload
        session_key = payload.get("session")
        job = payload.get("job")
        kind = payload.get("kind")
        if not (isinstance(session_key, str) and isinstance(job, str)):
            logger.warning("Dropping malformed generation message")
            return
        if kind == "structure":
            self._run(
                message,
                start=lambda: self._structures.mark_running(session_key, job),
                work=lambda session: self._propose(session, job),
                fail=lambda text: self._structures.fail_job(session_key, job, text),
                session_key=session_key,
            )
        elif kind == "content" and isinstance(payload.get("node"), str):
            node = payload["node"]
            self._run(
                message,
                start=lambda: self._contents.mark_running(session_key, node, job),
                work=lambda session: self._generate(session, node, job),
                fail=lambda text: self._contents.fail_job(session_key, node, job, text),
                session_key=session_key,
            )
        else:
            logger.warning("Dropping generation message of unknown kind")

    def _run(
        self,
        message: QueueMessage,
        *,
        start: Callable[[], bool],
        work: Callable[[WorkspaceSession], None],
        fail: Callable[[str], None],
        session_key: str,
    ) -> None:
        session = self._sessions.get_active(session_key)
        if session is None:
            return
        if message.dequeue_count > self._settings.max_job_attempts:
            fail("Processing failed repeatedly. Try again later.")
            return
        if not start():
            return
        try:
            work(session)
        except (ModelError, JobFailedError) as error:
            code = getattr(error, "code", "failed")
            logger.info("Generation job failed: %s", code)
            fail(error.message)
        except Exception as error:
            logger.error("Generation job failed unexpectedly: %s", type(error).__name__)
            fail("Processing failed unexpectedly. Try again.")

    def _model_client(self) -> ModelClient:
        if self._model is None:
            self._model = self._model_factory()
        return self._model

    def _call(self, session: WorkspaceSession, messages: list[dict[str, str]], schema: Any) -> Any:
        prompt_tokens = sum(estimate_tokens(message["content"]) for message in messages)
        needed = prompt_tokens + self._settings.model_max_output_tokens
        if session.model_tokens_used + needed > self._settings.max_session_model_tokens:
            raise JobFailedError(
                "This session has used up its AI allowance, so this request was not sent. "
                "Content you already have stays available; start a new session to generate more."
            )
        result = self._model_client().parse(messages, schema)
        self._sessions.add_model_tokens(session.key, result.tokens or needed)
        return result.parsed

    def _require_sources(
        self, session: WorkspaceSession
    ) -> tuple[ProjectContext, list[SourceMaterial], ChunkIndex]:
        if session.context is None or not session.sources_approved:
            raise JobFailedError("The sources changed. Approve them again and retry.")
        materials, index = load_source_material(self._sources, self._blobs, session.key)
        if not materials:
            raise JobFailedError("No converted sources are available.")
        return session.context, materials, index

    def _propose(self, session: WorkspaceSession, job: str) -> None:
        context, materials, index = self._require_sources(session)
        state, current = self._structures.draft(session.key)
        messages = structure_messages(
            context, materials, instructions=state.instructions, current=current
        )
        proposal = self._call(session, messages, StructureProposal)
        structure = proposal_to_structure(proposal, index)
        if not structure.categories:
            raise JobFailedError("The AI model did not propose any categories. Try again.")
        self._ensure_current(session)
        self._structures.complete_proposal(
            session.key,
            job,
            structure,
            sources_version=session.approved_sources_version,
            context_version=session.context_version,
            notes=" ".join(proposal.notes.split())[:2000],
        )

    def _generate(self, session: WorkspaceSession, node_key: str, job: str) -> None:
        context, materials, index = self._require_sources(session)
        state, structure = self._structures.approved(session.key)
        if structure is None or not state.approved:
            raise JobFailedError("Approve the structure again and retry.")
        missing = JobFailedError("This sub-category is no longer in the approved structure.")
        try:
            category, subcategory = structure.find(node_key)
        except StructureError as error:
            raise missing from error
        if category is None:
            raise missing
        messages = content_messages(context, materials, structure, category, subcategory)
        draft = self._call(session, messages, SubcategoryDraft)
        content = check_draft(
            draft,
            index,
            generated_on=self._clock().date().isoformat(),
            prompt_version=PROMPT_VERSION,
        )
        self._ensure_current(session)
        if self._structures.state(session.key).approved_version != state.approved_version:
            raise JobFailedError("The structure changed while this was running. Try again.")
        self._contents.complete(
            session.key,
            node_key,
            job,
            content,
            structure_version=state.approved_version,
            sources_version=session.approved_sources_version,
            context_version=session.context_version,
        )

    def _ensure_current(self, session: WorkspaceSession) -> None:
        latest = self._sessions.get_active(session.key)
        if not (
            latest is not None
            and latest.sources_approved
            and latest.approved_sources_version == session.approved_sources_version
            and latest.context_version == session.context_version
        ):
            raise JobFailedError(
                "The sources or context changed while this was running. Try again."
            )
