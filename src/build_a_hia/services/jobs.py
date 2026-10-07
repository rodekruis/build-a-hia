"""Background processing of source fetch and conversion jobs.

Each queue message names one source version. A job execution receives one message,
processes it and deletes it. Messages for deleted, expired or superseded work are dropped,
and results are only committed while the session and source version are still current.
"""

import json
import logging
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .chunking import build_chunks
from .conversion import ConversionCancelledError, ConversionOutput, ProgressCallback
from .inspection import FILE_EXTENSIONS, SourceError, SourceFormat, inspect_content
from .sessions import SessionService, utcnow
from .settings import Settings
from .sources import (
    ACTIVE_STATUSES,
    Source,
    SourceKind,
    SourceRepository,
    SourceStatus,
    chunks_blob,
    clean_title,
    markdown_blob,
    original_blob,
    source_blob_prefix,
)
from .storage import BlobStore, JobQueue, NotFoundError, QueueMessage, TableStore
from .url_fetch import FetchResult

logger = logging.getLogger(__name__)


class Converter(Protocol):
    """Converts a source file into markdown units."""

    def convert(
        self,
        path: Path,
        source_format: SourceFormat,
        progress: ProgressCallback,
        *,
        base_url: str = "",
    ) -> ConversionOutput:
        """Convert the file at `path`, reporting progress; raises `SourceError` on failure.

        `base_url` is the address of a fetched web page, used only to resolve its links.
        """
        ...


class Fetcher(Protocol):
    """Downloads web addresses safely."""

    def fetch(self, url: str) -> FetchResult:
        """Download `url`; raises `SourceError` for user-facing failures."""
        ...


class _SupersededError(Exception):
    """The session or source changed; results must be discarded."""


class ConversionProcessor:
    """Run queued fetch and conversion jobs for sources.

    Args:
        table: Table store holding sessions and their sources.
        blobs: Blob store for original files, markdown and chunks.
        queue: Queue with one message per source version to process.
        sessions: Session service used to check sessions and bump versions.
        settings: Application settings with job and source limits.
        converter: Converts source files to markdown.
        fetcher: Downloads web address sources.
        clock: Returns the current UTC time.
    """

    def __init__(
        self,
        *,
        table: TableStore,
        blobs: BlobStore,
        queue: JobQueue,
        sessions: SessionService,
        settings: Settings,
        converter: Converter,
        fetcher: Fetcher,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sources = SourceRepository(table)
        self._blobs = blobs
        self._queue = queue
        self._sessions = sessions
        self._settings = settings
        self._converter = converter
        self._fetcher = fetcher
        self._clock = clock

    def process_next(self) -> bool:
        """Receive and process one queued job message.

        The message is deleted afterwards, whatever the outcome. Messages for expired
        sessions, deleted sources or superseded versions are dropped, and a message delivered
        more than ``max_job_attempts`` times marks its source as failed. Processing errors are
        recorded on the source rather than raised.

        Returns:
            False when the queue is empty, otherwise True.
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
        source_id = payload.get("source")
        version = payload.get("version")
        if not (
            isinstance(session_key, str) and isinstance(source_id, str) and isinstance(version, int)
        ):
            logger.warning("Dropping malformed job message")
            return
        if self._sessions.get_active(session_key) is None:
            return
        source = self._sources.get(session_key, source_id)
        if source is None or source.version != version or source.status not in ACTIVE_STATUSES:
            return
        if message.dequeue_count > self._settings.max_job_attempts:
            self._fail(
                session_key,
                source_id,
                version,
                SourceError("repeated_failure", "Processing failed repeatedly. Try again later."),
            )
            return

        work_dir = Path(tempfile.mkdtemp(prefix="hia-"))
        try:
            self._process(session_key, source, work_dir)
        except (ConversionCancelledError, _SupersededError):
            logger.info("Discarded superseded job for source %s", source_id)
        except SourceError as error:
            logger.info("Source %s failed: %s", source_id, error.code)
            self._fail(session_key, source_id, version, error)
        except Exception as error:
            logger.error("Source %s failed unexpectedly: %s", source_id, type(error).__name__)
            self._fail(
                session_key,
                source_id,
                version,
                SourceError("internal_error", "Processing failed unexpectedly. Try again."),
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _process(self, session_key: str, source: Source, work_dir: Path) -> None:
        version = source.version
        if source.kind == SourceKind.URL:
            data = self._fetch(session_key, source)
        else:
            try:
                data = self._blobs.get(original_blob(session_key, source.id))
            except NotFoundError as error:
                raise SourceError(
                    "missing_original", "The uploaded file is no longer available. Add it again."
                ) from error

        source = self._update(
            session_key, source.id, version, status=SourceStatus.CONVERTING, pages_done=0
        )
        source_format = SourceFormat(source.format)
        path = work_dir / f"source{FILE_EXTENSIONS[source_format]}"
        path.write_bytes(data)

        def progress(done: int, _total: int) -> None:
            self._update(session_key, source.id, version, pages_done=done)

        output = self._converter.convert(
            path, source_format, progress, base_url=source.final_url or source.url
        )
        chunks = build_chunks(source.number, output.units)
        if not chunks:
            raise SourceError("no_text", "No readable text was found.")

        self._ensure_current(session_key, source.id, version)
        markdown = "\n\n".join(unit.markdown for unit in output.units)
        self._blobs.put(markdown_blob(session_key, source.id), markdown.encode("utf-8"))
        self._blobs.put(
            chunks_blob(session_key, source.id),
            json.dumps([chunk.to_dict() for chunk in chunks], ensure_ascii=False).encode("utf-8"),
        )
        try:
            self._update(
                session_key,
                source.id,
                version,
                status=SourceStatus.CONVERTED,
                pages_done=source.page_count,
                chunk_count=len(chunks),
                token_count=sum(chunk.tokens for chunk in chunks),
                limitations=output.limitations,
                error_code="",
                error_message="",
            )
        except _SupersededError:
            if self._sources.get(session_key, source.id) is None:
                self._blobs.delete_prefix(source_blob_prefix(session_key, source.id))
            raise
        self._blobs.delete_prefix(original_blob(session_key, source.id))
        self._sessions.bump_sources_version(session_key)

    def _fetch(self, session_key: str, source: Source) -> bytes:
        settings = self._settings
        self._update(session_key, source.id, source.version, status=SourceStatus.FETCHING)
        result = self._fetcher.fetch(source.url)
        inspected = inspect_content(
            result.data,
            max_pages=settings.max_document_pages,
            content_type=result.content_type,
            allow_html=True,
        )
        others = [other for other in self._sources.list_for(session_key) if other.id != source.id]
        if sum(other.size_bytes for other in others) + len(result.data) > (
            settings.max_session_bytes
        ):
            raise SourceError(
                "session_too_large", "This download would exceed the session's storage limit."
            )
        if sum(other.page_count for other in others) + inspected.page_count > (
            settings.max_session_pages
        ):
            raise SourceError(
                "session_too_many_pages",
                f"This document would exceed the limit of {settings.max_session_pages} pages.",
            )

        changes: dict[str, Any] = {
            "final_url": result.final_url,
            "retrieved_at": self._clock().isoformat(),
            "format": inspected.format.value,
            "size_bytes": len(result.data),
            "page_count": inspected.page_count,
        }
        if not source.title:
            changes["title"] = clean_title(inspected.title) or result.final_url
        if source.document_date_origin != "user":
            if inspected.document_date:
                changes["document_date"] = inspected.document_date
                changes["document_date_origin"] = "page metadata"
            elif result.last_modified and inspected.format != SourceFormat.HTML:
                changes["document_date"] = result.last_modified
                changes["document_date_origin"] = "server"
        self._ensure_current(session_key, source.id, source.version)
        self._blobs.put(original_blob(session_key, source.id), result.data)
        self._update(session_key, source.id, source.version, **changes)
        return result.data

    def _update(self, session_key: str, source_id: str, version: int, **changes: Any) -> Source:
        if self._sessions.get_active(session_key) is None:
            raise _SupersededError

        def apply(source: Source) -> bool:
            if source.version != version or source.status not in ACTIVE_STATUSES:
                raise _SupersededError
            for name, value in changes.items():
                setattr(source, name, value)
            return True

        updated = self._sources.mutate(session_key, source_id, apply)
        if updated is None:
            raise _SupersededError
        return updated

    def _ensure_current(self, session_key: str, source_id: str, version: int) -> None:
        if self._sessions.get_active(session_key) is None:
            raise _SupersededError
        source = self._sources.get(session_key, source_id)
        if source is None or source.version != version or source.status not in ACTIVE_STATUSES:
            raise _SupersededError

    def _fail(self, session_key: str, source_id: str, version: int, error: SourceError) -> None:
        changed: list[bool] = []

        def apply(source: Source) -> bool:
            if source.version != version or source.status not in ACTIVE_STATUSES:
                return False
            source.status = SourceStatus.FAILED
            source.error_code = error.code
            source.error_message = error.message
            changed.append(True)
            return True

        if self._sessions.get_active(session_key) is None:
            return
        self._sources.mutate(session_key, source_id, apply)
        if changed:
            self._sessions.bump_sources_version(session_key)
