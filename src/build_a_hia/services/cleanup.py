"""Periodic cleanup of expired sessions and session data left without a session."""

from dataclasses import dataclass

from .sessions import SESSION_ROW, SessionService, WorkspaceSession
from .storage import BlobStore, TableStore


@dataclass(frozen=True)
class CleanupReport:
    """What a cleanup run deleted.

    Attributes:
        expired_sessions: Expired sessions deleted with all their rows and blobs.
        orphaned_partitions: Table partitions deleted because they had no session row.
        orphaned_blob_sessions: Session blob folders deleted because they had no session row.
    """

    expired_sessions: int
    orphaned_partitions: int
    orphaned_blob_sessions: int


class CleanupService:
    """Deletes expired sessions and anything left behind by interrupted work.

    Args:
        table: Table store holding sessions and their entities.
        blobs: Blob store holding session files.
        sessions: Session service used to detect and delete expired sessions.
    """

    def __init__(self, table: TableStore, blobs: BlobStore, sessions: SessionService) -> None:
        self._table = table
        self._blobs = blobs
        self._sessions = sessions

    def run(self) -> CleanupReport:
        """Run one cleanup pass.

        Expired sessions are deleted with all their table rows and blobs. Table partitions
        and ``sessions/<key>/`` blob folders without a session row are deleted too; each is
        checked again just before deletion so that sessions created meanwhile are kept.

        Returns:
            Counts of what was deleted.
        """
        live: set[str] = set()
        expired = 0
        for key, stored in self._table.list_by_row(SESSION_ROW):
            if self._sessions.is_expired(WorkspaceSession.from_stored(key, stored)):
                self._sessions.delete(key)
                expired += 1
            else:
                live.add(key)

        orphaned_partitions = 0
        for key in self._table.list_partitions() - live:
            if self._table.get(key, SESSION_ROW) is not None:
                continue
            for row, _entity in self._table.list_partition(key):
                self._table.delete(key, row)
            orphaned_partitions += 1

        orphaned_blobs = 0
        for key in self._blobs.list_children("sessions/") - live:
            if self._table.get(key, SESSION_ROW) is not None:
                continue
            self._blobs.delete_prefix(f"sessions/{key}/")
            orphaned_blobs += 1

        return CleanupReport(
            expired_sessions=expired,
            orphaned_partitions=orphaned_partitions,
            orphaned_blob_sessions=orphaned_blobs,
        )
