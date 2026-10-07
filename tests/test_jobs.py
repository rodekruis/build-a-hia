import json

import pytest
from factories import make_text_pdf, session_key_of, upload

from build_a_hia.services.chunking import ConvertedUnit
from build_a_hia.services.conversion import ConversionOutput
from build_a_hia.services.inspection import SourceError
from build_a_hia.services.jobs import ConversionProcessor
from build_a_hia.services.sources import (
    SourceStatus,
    chunks_blob,
    markdown_blob,
    original_blob,
)
from build_a_hia.services.storage import NotFoundError
from build_a_hia.services.url_fetch import FetchResult


class FakeConverter:
    def __init__(self, units=None, error=None, on_progress=None):
        self.units = units or [ConvertedUnit(page=1, markdown="Shelter at the station.")]
        self.error = error
        self.on_progress = on_progress
        self.calls = 0
        self.base_urls: list[str] = []

    def convert(self, path, source_format, progress, *, base_url=""):
        self.calls += 1
        self.base_urls.append(base_url)
        assert path.exists()
        progress(0, 2)
        if self.on_progress:
            self.on_progress()
        progress(1, 2)
        if self.error:
            raise self.error
        return ConversionOutput(units=self.units, limitations=["Page 2 was blank."])


class FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def fetch(self, url):
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def make_processor(services):
    def build(converter=None, fetcher=None):
        return ConversionProcessor(
            table=services.table,
            blobs=services.blobs,
            queue=services.queue,
            sessions=services.sessions,
            settings=services.settings,
            converter=converter or FakeConverter(),
            fetcher=fetcher or FakeFetcher(),
        )

    return build


@pytest.fixture
def uploaded(workspace_client, services, pdf_bytes):
    upload(workspace_client, "shelter.pdf", pdf_bytes)
    key = session_key_of(workspace_client)
    [source] = services.sources.list_for(key)
    return key, source


def test_converted_source_is_chunked_and_original_removed(services, make_processor, uploaded):
    key, source = uploaded

    assert make_processor().process_next() is True

    converted = services.sources.get(key, source.id)
    assert converted.status == SourceStatus.CONVERTED
    assert converted.chunk_count == 1
    assert converted.token_count > 0
    assert converted.limitations == ["Page 2 was blank."]
    chunks = json.loads(services.blobs.get(chunks_blob(key, source.id)))
    assert chunks[0]["id"] == "S1-p1-c1"
    assert services.blobs.get(markdown_blob(key, source.id)) == b"Shelter at the station."
    with pytest.raises(NotFoundError):
        services.blobs.get(original_blob(key, source.id))
    assert services.queue.receive(60) is None


def test_empty_queue_returns_false(make_processor):
    assert make_processor().process_next() is False


def test_conversion_invalidates_source_approval(services, make_processor, uploaded):
    key, _source = uploaded
    before = services.sessions.get_active(key).sources_version

    make_processor().process_next()

    assert services.sessions.get_active(key).sources_version > before


def test_superseded_message_is_dropped(services, make_processor, uploaded):
    key, source = uploaded
    services.sources.cancel(key, source.id)
    services.sources.retry(key, source.id)
    converter = FakeConverter()
    processor = make_processor(converter)

    processor.process_next()
    assert converter.calls == 0
    processor.process_next()

    assert converter.calls == 1
    assert services.sources.get(key, source.id).status == SourceStatus.CONVERTED


def test_message_for_deleted_session_is_dropped(services, make_processor, uploaded):
    key, _source = uploaded
    services.sessions.delete(key)
    converter = FakeConverter()

    assert make_processor(converter).process_next() is True

    assert converter.calls == 0
    assert services.blobs.list_children("sessions/") == set()


def test_cancel_during_conversion_discards_results(services, make_processor, uploaded):
    key, source = uploaded
    converter = FakeConverter(on_progress=lambda: services.sources.cancel(key, source.id))

    make_processor(converter).process_next()

    cancelled = services.sources.get(key, source.id)
    assert cancelled.status == SourceStatus.CANCELLED
    with pytest.raises(NotFoundError):
        services.blobs.get(chunks_blob(key, source.id))


def test_removal_during_conversion_leaves_no_files(services, make_processor, uploaded):
    key, source = uploaded
    converter = FakeConverter(on_progress=lambda: services.sources.remove(key, source.id))

    make_processor(converter).process_next()

    assert services.sources.get(key, source.id) is None
    assert services.blobs.list_children(f"sessions/{key}/sources/") == set()


def test_deleting_session_during_conversion_prevents_recreation(services, make_processor, uploaded):
    key, _source = uploaded
    converter = FakeConverter(on_progress=lambda: services.sessions.delete(key))

    make_processor(converter).process_next()

    assert services.table.list_partition(key) == []
    assert services.blobs.list_children("sessions/") == set()


def test_conversion_error_marks_source_failed(services, make_processor, uploaded):
    key, source = uploaded
    converter = FakeConverter(error=SourceError("no_text", "No readable text was found."))

    make_processor(converter).process_next()

    failed = services.sources.get(key, source.id)
    assert failed.status == SourceStatus.FAILED
    assert failed.error_message == "No readable text was found."
    assert services.blobs.get(original_blob(key, source.id))


def test_unexpected_error_is_reported_without_details(services, make_processor, uploaded):
    key, source = uploaded
    converter = FakeConverter(error=RuntimeError("secret document text"))

    make_processor(converter).process_next()

    failed = services.sources.get(key, source.id)
    assert failed.error_code == "internal_error"
    assert "secret" not in failed.error_message


def test_repeated_deliveries_fail_the_source(services, make_processor, uploaded, monkeypatch):
    key, source = uploaded
    receive = services.queue.receive

    def redelivered(visibility_seconds):
        message = receive(visibility_seconds)
        message.dequeue_count = services.settings.max_job_attempts + 1
        return message

    monkeypatch.setattr(services.queue, "receive", redelivered)
    converter = FakeConverter()

    make_processor(converter).process_next()

    assert converter.calls == 0
    assert services.sources.get(key, source.id).error_code == "repeated_failure"


def test_url_source_is_fetched_and_described(workspace_client, services, make_processor):
    workspace_client.post("/sources/url", data={"url": "https://example.org/help"})
    key = session_key_of(workspace_client)
    page = (
        b"<html><head><title>Cash help</title>"
        b'<meta name="date" content="2026-09-01"></head><body>Cash</body></html>'
    )
    fetcher = FakeFetcher(
        FetchResult(
            final_url="https://example.org/en/help",
            content_type="text/html",
            data=page,
            last_modified="2026-10-07",
        )
    )

    converter = FakeConverter()
    make_processor(converter=converter, fetcher=fetcher).process_next()

    [source] = services.sources.list_for(key)
    assert converter.base_urls == ["https://example.org/en/help"]
    assert source.status == SourceStatus.CONVERTED
    assert source.format == "html"
    assert source.title == "Cash help"
    assert source.final_url == "https://example.org/en/help"
    assert source.document_date == "2026-09-01"
    assert source.document_date_origin == "page metadata"
    assert source.retrieved_at


def test_fetched_pdf_uses_server_date(workspace_client, services, make_processor):
    workspace_client.post("/sources/url", data={"url": "https://example.org/guide.pdf"})
    key = session_key_of(workspace_client)
    fetcher = FakeFetcher(
        FetchResult(
            final_url="https://example.org/guide.pdf",
            content_type="application/pdf",
            data=make_text_pdf(["Guide"]),
            last_modified="2026-05-05",
        )
    )

    make_processor(fetcher=fetcher).process_next()

    [source] = services.sources.list_for(key)
    assert source.format == "pdf"
    assert source.page_count == 1
    assert source.document_date == "2026-05-05"
    assert source.document_date_origin == "server"


def test_blocked_url_marks_source_failed(workspace_client, services, make_processor):
    workspace_client.post("/sources/url", data={"url": "https://example.org/"})
    key = session_key_of(workspace_client)
    fetcher = FakeFetcher(
        error=SourceError("blocked_address", "Only public websites can be added.")
    )

    make_processor(fetcher=fetcher).process_next()

    [source] = services.sources.list_for(key)
    assert source.status == SourceStatus.FAILED
    assert source.error_code == "blocked_address"
