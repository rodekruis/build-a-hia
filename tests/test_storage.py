import os
import stat

import pytest

from build_a_hia.services.storage import (
    ConflictError,
    LocalBlobStore,
    LocalJobQueue,
    LocalTableStore,
    NotFoundError,
)


def test_table_updates_are_conditional(tmp_path):
    table = LocalTableStore(tmp_path)
    etag = table.insert("p", "source:a", {"value": 1, "empty": None})

    stored = table.get("p", "source:a")
    assert stored is not None
    assert stored.data == {"value": 1}
    new_etag = table.update("p", "source:a", {"value": 2}, etag)
    with pytest.raises(ConflictError):
        table.update("p", "source:a", {"value": 3}, etag)
    with pytest.raises(ConflictError):
        table.insert("p", "source:a", {})
    updated = table.get("p", "source:a")
    assert updated is not None
    assert updated.etag == new_etag
    assert [row for row, _entity in table.list_partition("p")] == ["source:a"]


def test_blob_names_cannot_escape_root(tmp_path):
    blobs = LocalBlobStore(tmp_path)

    with pytest.raises(ValueError):
        blobs.put("sessions/../../outside", b"x")
    with pytest.raises(ValueError):
        blobs.put("sessions/a\\..\\..\\outside", b"x")
    with pytest.raises(ValueError):
        blobs.put("sessions/C:outside", b"x")
    with pytest.raises(NotFoundError):
        blobs.get("sessions/missing")


def test_delete_prefix_removes_nested_blobs(tmp_path):
    blobs = LocalBlobStore(tmp_path)
    blobs.put("sessions/a/sources/s1/original", b"x")
    blobs.put("sessions/a/structure/v1", b"y")
    blobs.put("sessions/b/structure/v1", b"z")

    blobs.delete_prefix("sessions/a/")

    assert not (tmp_path / "sessions" / "a").exists()
    assert blobs.get("sessions/b/structure/v1") == b"z"


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses to remove read-only folders")
def test_delete_prefix_handles_read_only_folders(tmp_path):
    blobs = LocalBlobStore(tmp_path)
    blobs.put("sessions/a/structure/v1", b"y")
    (tmp_path / "sessions" / "a" / "structure").chmod(stat.S_IREAD)

    blobs.delete_prefix("sessions/a/")

    assert not (tmp_path / "sessions" / "a").exists()


def test_queue_hides_received_messages_and_counts_deliveries(tmp_path):
    queue = LocalJobQueue(tmp_path)
    queue.send({"n": 1})

    first = queue.receive(visibility_seconds=0)
    second = queue.receive(visibility_seconds=60)

    assert first is not None
    assert second is not None
    assert first.dequeue_count == 1
    assert second.dequeue_count == 2
    assert queue.receive(visibility_seconds=60) is None
    queue.delete(first)
    assert len(list(tmp_path.glob("*.json"))) == 1
    queue.delete(second)
    assert list(tmp_path.glob("*.json")) == []
