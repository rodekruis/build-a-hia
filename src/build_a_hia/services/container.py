"""Wire the storage backend and services together for the web app and the worker."""

from dataclasses import dataclass
from pathlib import Path

from .drafting import ContentService, StructureService
from .export import ExportService
from .review import GapStore
from .sessions import SessionService
from .settings import Settings
from .sources import SourceService
from .storage import (
    BlobStore,
    JobQueue,
    LocalBlobStore,
    LocalJobQueue,
    LocalTableStore,
    TableStore,
)


@dataclass
class Storage:
    """The storage backends: table, blobs, and the conversion and generation job queues."""

    table: TableStore
    blobs: BlobStore
    convert_queue: JobQueue
    generate_queue: JobQueue


@dataclass
class Services:
    """All services of the application, sharing one storage backend."""

    settings: Settings
    storage: Storage
    sessions: SessionService
    sources: SourceService
    structures: StructureService
    contents: ContentService
    gaps: GapStore
    exports: ExportService

    @property
    def table(self) -> TableStore:
        """The table store."""
        return self.storage.table

    @property
    def blobs(self) -> BlobStore:
        """The blob store."""
        return self.storage.blobs

    @property
    def queue(self) -> JobQueue:
        """The source conversion job queue."""
        return self.storage.convert_queue


def build_storage(settings: Settings) -> Storage:
    """Create the local or Azure storage backend selected in the settings.

    Args:
        settings: Application settings.

    Returns:
        The storage backends.

    Raises:
        RuntimeError: If the storage backend setting is neither "local" nor "azure".
    """
    if settings.storage_backend == "local":
        root = Path(settings.local_storage_dir)
        return Storage(
            table=LocalTableStore(root / "tables"),
            blobs=LocalBlobStore(root / "blobs"),
            convert_queue=LocalJobQueue(root / "queues" / settings.convert_queue),
            generate_queue=LocalJobQueue(root / "queues" / settings.generate_queue),
        )
    if settings.storage_backend == "azure":
        from .storage_azure import build_azure_storage

        return Storage(*build_azure_storage(settings))
    raise RuntimeError("HIA_STORAGE_BACKEND must be 'local' or 'azure'")


def build_services(settings: Settings) -> Services:
    """Create the storage backend and all services that use it.

    Args:
        settings: Application settings.

    Returns:
        The wired services.

    Raises:
        RuntimeError: If the storage backend setting is neither "local" nor "azure".
    """
    storage = build_storage(settings)
    sessions = SessionService(storage.table, storage.blobs, settings)
    structures = StructureService(storage.table, storage.blobs, storage.generate_queue, settings)
    contents = ContentService(
        storage.table, storage.blobs, storage.generate_queue, structures, settings
    )
    return Services(
        settings=settings,
        storage=storage,
        sessions=sessions,
        sources=SourceService(
            storage.table, storage.blobs, storage.convert_queue, sessions, settings
        ),
        structures=structures,
        contents=contents,
        gaps=GapStore(storage.table),
        exports=ExportService(storage.table, storage.blobs, structures, contents),
    )
