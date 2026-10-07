from datetime import UTC, datetime, timedelta

import pytest

from build_a_hia.services.cleanup import CleanupService
from build_a_hia.services.container import build_storage
from build_a_hia.services.sessions import SESSION_ROW, SessionService, session_key
from build_a_hia.services.storage import NotFoundError


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def storage(settings):
    return build_storage(settings)


@pytest.fixture
def sessions(storage, settings, clock):
    return SessionService(storage.table, storage.blobs, settings, clock=clock)


def test_session_is_stored_under_token_hash(sessions, storage):
    token, workspace = sessions.create()

    assert workspace.key == session_key(token)
    assert storage.table.get(token, SESSION_ROW) is None
    assert sessions.load(token).key == workspace.key


def test_session_expires_after_idle_period(sessions, clock):
    token, _workspace = sessions.create()

    clock.advance(hours=1, minutes=59)
    assert sessions.load(token) is not None
    clock.advance(minutes=1)

    assert sessions.load(token) is None


def test_activity_extends_idle_but_not_absolute_lifetime(sessions, clock):
    token, _workspace = sessions.create()
    for _ in range(12):
        clock.advance(hours=1, minutes=55)
        workspace = sessions.load(token)
        assert workspace is not None
        sessions.touch(workspace)

    clock.advance(hours=1)

    assert sessions.load(token) is None


def test_touch_is_throttled(sessions, clock):
    token, workspace = sessions.create()
    clock.advance(seconds=30)

    assert sessions.touch(workspace).last_seen_at == workspace.created_at
    clock.advance(seconds=31)
    assert sessions.touch(workspace).last_seen_at == clock.now


def test_expired_session_content_is_deleted_on_access(sessions, storage, clock):
    table, blobs = storage.table, storage.blobs
    token, workspace = sessions.create()
    blobs.put(f"sessions/{workspace.key}/sources/a/original", b"data")
    clock.advance(hours=3)

    assert sessions.load(token) is None
    assert blobs.list_children("sessions/") == set()
    assert table.list_partition(workspace.key) == []


def test_mutating_expired_session_fails(sessions, clock):
    _token, workspace = sessions.create()
    clock.advance(hours=3)

    with pytest.raises(NotFoundError):
        sessions.mutate(workspace.key, lambda session: None)


def test_overlong_tokens_are_rejected(sessions):
    assert sessions.load("x" * 500) is None


def test_cleanup_removes_expired_and_orphaned_data(sessions, storage, clock):
    table, blobs = storage.table, storage.blobs
    _old_token, old = sessions.create()
    blobs.put(f"sessions/{old.key}/sources/a/original", b"old")
    clock.advance(hours=1)
    _live_token, live = sessions.create()
    blobs.put(f"sessions/{live.key}/sources/b/original", b"live")
    table.insert("orphan", "source:x", {"number": 1})
    blobs.put("sessions/orphan/sources/x/original", b"orphan")
    clock.advance(hours=1, minutes=30)

    report = CleanupService(table, blobs, sessions).run()

    assert report.expired_sessions == 1
    assert report.orphaned_partitions == 1
    assert report.orphaned_blob_sessions == 1
    assert table.get(live.key, SESSION_ROW) is not None
    assert blobs.list_children("sessions/") == {live.key}
