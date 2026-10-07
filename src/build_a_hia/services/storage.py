"""Storage interfaces for workflow state, temporary artifacts and job messages.

The local implementations keep everything on disk for development and tests. They are
not safe for concurrent use by multiple machines; deployments use the Azure backend.
"""

import json
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote


class StorageError(Exception):
    """Base class for storage backend errors."""


class ConflictError(StorageError):
    """The entity already exists or was changed by someone else."""


class NotFoundError(StorageError):
    """The requested entity or blob does not exist."""


@dataclass
class StoredEntity:
    """A table entity together with its concurrency token.

    Attributes:
        data: Entity properties, without partition and row keys. `None` values are not stored.
        etag: Opaque version token to pass to `TableStore.update` for optimistic concurrency.
    """

    data: dict[str, Any]
    etag: str


@dataclass
class QueueMessage:
    """A message received from a job queue.

    Attributes:
        id: Backend message identifier.
        pop_receipt: Token from the latest receive; required to delete the message.
        dequeue_count: Number of times the message has been received, including this one.
        payload: Decoded JSON payload.
    """

    id: str
    pop_receipt: str
    dequeue_count: int
    payload: dict[str, Any]


class TableStore(Protocol):
    """Key-value table of entities addressed by partition and row key."""

    def get(self, partition: str, row: str) -> StoredEntity | None:
        """Return the entity, or None when it does not exist."""

    def insert(self, partition: str, row: str, data: dict[str, Any]) -> str:
        """Create a new entity.

        Args:
            partition: Partition key.
            row: Row key.
            data: Entity properties; `None` values are dropped.

        Returns:
            The etag of the new entity.

        Raises:
            ConflictError: The entity already exists.
        """

    def update(self, partition: str, row: str, data: dict[str, Any], etag: str) -> str:
        """Replace an entity if it has not changed since it was read.

        Args:
            partition: Partition key.
            row: Row key.
            data: New entity properties, replacing all existing ones; `None` values are dropped.
            etag: Etag from the read the update is based on.

        Returns:
            The new etag.

        Raises:
            ConflictError: The entity was modified since `etag` was issued.
            NotFoundError: The entity does not exist.
        """

    def delete(self, partition: str, row: str) -> None:
        """Delete the entity; does nothing when it does not exist."""

    def list_partition(self, partition: str) -> list[tuple[str, StoredEntity]]:
        """Return `(row, entity)` pairs for every entity in a partition."""

    def list_by_row(self, row: str) -> list[tuple[str, StoredEntity]]:
        """Return `(partition, entity)` pairs for every entity with the given row key."""

    def list_partitions(self) -> set[str]:
        """Return the keys of all partitions that contain at least one entity."""


class BlobStore(Protocol):
    """Store for binary artifacts addressed by `/`-separated names."""

    def put(self, name: str, data: bytes) -> None:
        """Write a blob, overwriting any existing blob with the same name."""

    def get(self, name: str) -> bytes:
        """Return the blob contents.

        Args:
            name: Blob name.

        Returns:
            The stored bytes.

        Raises:
            NotFoundError: The blob does not exist.
        """

    def delete_prefix(self, prefix: str) -> None:
        """Delete every blob under `prefix`; does nothing when there are none."""

    def list_children(self, prefix: str) -> set[str]:
        """Return the names of the direct children (blobs and folders) under `prefix`."""


class JobQueue(Protocol):
    """At-least-once message queue for background jobs."""

    def send(self, payload: dict[str, Any]) -> None:
        """Enqueue a JSON-serializable payload."""

    def receive(self, visibility_seconds: int) -> QueueMessage | None:
        """Receive the next visible message and hide it from other consumers.

        The message becomes visible again after `visibility_seconds` unless it is deleted.

        Args:
            visibility_seconds: How long the message stays hidden.

        Returns:
            The message, or None when no message is visible.
        """

    def delete(self, message: QueueMessage) -> None:
        """Remove a processed message; ignored when its pop receipt is no longer valid."""


_MAX_UPDATE_ATTEMPTS = 5


def mutate_entity(
    table: TableStore,
    partition: str,
    row: str,
    change: Callable[[dict[str, Any]], bool],
    *,
    create: bool = False,
) -> dict[str, Any] | None:
    """Read, modify and write an entity with optimistic concurrency.

    `change` is re-run on fresh data whenever the write hits an etag conflict.

    Args:
        table: Table holding the entity.
        partition: Partition key.
        row: Row key.
        change: Mutates the data in place; returns False to skip the write.
        create: Insert the entity (starting from empty data) when it does not exist.

    Returns:
        The resulting data (unchanged data when the write was skipped), or None when the
        entity is missing and `create` is False, or was deleted during the update.

    Raises:
        ConflictError: The write kept conflicting after several attempts.
    """
    for _ in range(_MAX_UPDATE_ATTEMPTS):
        stored = table.get(partition, row)
        if stored is None and not create:
            return None
        data = dict(stored.data) if stored else {}
        if not change(data):
            return data
        try:
            if stored is None:
                table.insert(partition, row, data)
            else:
                table.update(partition, row, data, stored.etag)
        except ConflictError:
            continue
        except NotFoundError:
            return None
        return data
    raise ConflictError("Busy; try again")


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    # Azure Tables cannot store None; omitting keeps both backends consistent.
    return {key: value for key, value in data.items() if value is not None}


def _replace(temporary: Path, path: Path) -> None:
    # Windows briefly locks files that virus scanners or sync clients are reading.
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def _retry_writable(function: Callable[[str], Any], path: str, error: BaseException) -> None:
    # OneDrive marks synced folders read-only, and scanners briefly lock files on Windows.
    if isinstance(error, FileNotFoundError):
        return
    if not isinstance(error, PermissionError):
        raise error
    for attempt in range(10):
        try:
            Path(path).chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    _replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


class LocalTableStore:
    """`TableStore` that keeps one JSON file per entity on disk.

    Writes are serialized with a process-local lock only.

    Args:
        root: Directory holding one sub-directory per partition.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _path(self, partition: str, row: str) -> Path:
        return self._root / quote(partition, safe="") / f"{quote(row, safe='')}.json"

    def get(self, partition: str, row: str) -> StoredEntity | None:
        """Return the entity, or None when it does not exist."""
        stored = _read_json(self._path(partition, row))
        if stored is None:
            return None
        return StoredEntity(data=stored["data"], etag=stored["etag"])

    def insert(self, partition: str, row: str, data: dict[str, Any]) -> str:
        """Create a new entity and return its etag; see `TableStore.insert`."""
        with self._lock:
            path = self._path(partition, row)
            if path.exists():
                raise ConflictError("Entity already exists")
            etag = uuid.uuid4().hex
            _write_json(path, {"data": _clean(data), "etag": etag})
            return etag

    def update(self, partition: str, row: str, data: dict[str, Any], etag: str) -> str:
        """Replace an unchanged entity and return its new etag; see `TableStore.update`."""
        with self._lock:
            path = self._path(partition, row)
            stored = _read_json(path)
            if stored is None:
                raise NotFoundError("Entity not found")
            if stored["etag"] != etag:
                raise ConflictError("Entity was modified")
            new_etag = uuid.uuid4().hex
            _write_json(path, {"data": _clean(data), "etag": new_etag})
            return new_etag

    def delete(self, partition: str, row: str) -> None:
        """Delete the entity; does nothing when it does not exist."""
        with self._lock:
            self._path(partition, row).unlink(missing_ok=True)

    def list_partition(self, partition: str) -> list[tuple[str, StoredEntity]]:
        """Return `(row, entity)` pairs for a partition, sorted by file name."""
        folder = self._root / quote(partition, safe="")
        if not folder.is_dir():
            return []
        entities = []
        for path in sorted(folder.glob("*.json")):
            stored = _read_json(path)
            if stored is not None:
                row = unquote(path.stem)
                entities.append((row, StoredEntity(data=stored["data"], etag=stored["etag"])))
        return entities

    def list_by_row(self, row: str) -> list[tuple[str, StoredEntity]]:
        """Return `(partition, entity)` pairs for every entity with the given row key."""
        entities = []
        for partition in self.list_partitions():
            entity = self.get(partition, row)
            if entity is not None:
                entities.append((partition, entity))
        return entities

    def list_partitions(self) -> set[str]:
        """Return the keys of all partitions that contain at least one entity."""
        if not self._root.is_dir():
            return set()
        return {
            unquote(folder.name)
            for folder in self._root.iterdir()
            if folder.is_dir() and any(folder.glob("*.json"))
        }


class LocalBlobStore:
    """`BlobStore` that maps blob names to files under a root directory.

    Names with empty, `.` or `..` segments are rejected with `ValueError`.

    Args:
        root: Directory that holds the blobs.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, name: str) -> Path:
        parts = name.split("/")
        # Backslashes and drive colons would act as separators or roots on Windows.
        if any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
            raise ValueError("Invalid blob name")
        return self._root.joinpath(*parts)

    def put(self, name: str, data: bytes) -> None:
        """Atomically write a blob, overwriting any existing blob with the same name."""
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        temporary.write_bytes(data)
        _replace(temporary, path)

    def get(self, name: str) -> bytes:
        """Return the blob contents; raises `NotFoundError` when it does not exist."""
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError as error:
            raise NotFoundError(name) from error

    def delete_prefix(self, prefix: str) -> None:
        """Delete the blob or folder at `prefix`, including everything below it."""
        folder = self._path(prefix.rstrip("/"))
        if folder.is_file():
            folder.unlink(missing_ok=True)
            return
        if not folder.is_dir():
            return
        shutil.rmtree(folder, onexc=_retry_writable)

    def list_children(self, prefix: str) -> set[str]:
        """Return the names of the files and folders directly under `prefix`."""
        folder = self._path(prefix.rstrip("/"))
        if not folder.is_dir():
            return set()
        return {child.name for child in folder.iterdir()}


class LocalJobQueue:
    """`JobQueue` that stores one JSON file per message, received in send order.

    Args:
        root: Directory that holds the queue's messages.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        """Enqueue a JSON-serializable payload that is immediately visible."""
        message_id = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        _write_json(
            self._root / f"{message_id}.json",
            {"payload": payload, "visible_at": 0.0, "dequeue_count": 0, "pop_receipt": ""},
        )

    def receive(self, visibility_seconds: int) -> QueueMessage | None:
        """Receive the oldest visible message and hide it; see `JobQueue.receive`."""
        with self._lock:
            if not self._root.is_dir():
                return None
            now = time.time()
            for path in sorted(self._root.glob("*.json")):
                stored = _read_json(path)
                if stored is None or stored["visible_at"] > now:
                    continue
                stored["visible_at"] = now + visibility_seconds
                stored["dequeue_count"] += 1
                stored["pop_receipt"] = uuid.uuid4().hex
                _write_json(path, stored)
                return QueueMessage(
                    id=path.stem,
                    pop_receipt=stored["pop_receipt"],
                    dequeue_count=stored["dequeue_count"],
                    payload=stored["payload"],
                )
            return None

    def delete(self, message: QueueMessage) -> None:
        """Remove the message unless it was received again since (pop receipt changed)."""
        with self._lock:
            path = self._root / f"{message.id}.json"
            stored = _read_json(path)
            if stored is not None and stored["pop_receipt"] == message.pop_receipt:
                path.unlink(missing_ok=True)
