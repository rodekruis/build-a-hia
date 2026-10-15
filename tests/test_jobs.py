import io
import json
from types import SimpleNamespace

import pytest
from factories import make_png, make_text_pdf, session_key_of, upload
from PIL import Image

from build_a_hia import worker
from build_a_hia.services.chunking import ConvertedUnit, estimate_tokens
from build_a_hia.services.conversion import ConversionOutput, DoclingConverter
from build_a_hia.services.generation import load_source_material
from build_a_hia.services.inspection import SourceError, SourceFormat
from build_a_hia.services.jobs import ConversionProcessor
from build_a_hia.services.review import collect_gaps
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
        self.ocr_languages: list[str] = []

    def convert(self, path, source_format, progress, *, base_url="", ocr_language="en"):
        self.calls += 1
        self.base_urls.append(base_url)
        self.ocr_languages.append(ocr_language)
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


def test_selected_ocr_language_reaches_converter(services, make_processor, uploaded):
    key, source = uploaded
    services.sources.cancel(key, source.id)
    services.sources.update_metadata(
        key,
        source.id,
        title=source.title,
        document_date="",
        ocr_language="ar",
    )
    services.sources.retry(key, source.id)
    converter = FakeConverter()
    processor = make_processor(converter=converter)
    while processor.process_next():
        pass
    assert converter.ocr_languages == ["ar"]
    assert services.sources.get(key, source.id).ocr_language == "ar"


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


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_image_urls_use_image_conversion_with_the_selected_language(
    workspace_client, services, make_processor, image_format
):
    workspace_client.post(
        "/sources/url",
        data={"url": "https://example.org/image", "ocr_language": "ar"},
    )
    key = session_key_of(workspace_client)
    data = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(data, format=image_format)
    converter = FakeConverter()

    make_processor(
        converter=converter,
        fetcher=FakeFetcher(
            FetchResult("https://example.org/image", "image/*", data.getvalue(), "")
        ),
    ).process_next()

    [source] = services.sources.list_for(key)
    assert source.status == SourceStatus.CONVERTED
    assert source.format == image_format.lower()
    assert source.page_count == 1
    assert converter.ocr_languages == ["ar"]


@pytest.mark.parametrize("read_image", [True, False])
def test_worker_embedded_image_ocr_or_warnings_reach_chunks_and_review(
    workspace_client, services, monkeypatch, read_image
):
    page_url = "https://example.org/program/page.html"
    image_url = "https://example.org/program/Images/amounts.png"
    page = b'<main><p>Native service information.</p><img src="Images/amounts.png"></main>'
    workspace_client.post("/sources/url", data={"url": page_url, "ocr_language": "ar"})
    key = session_key_of(workspace_client)
    requests = []
    languages = []

    def safe_fetcher(**limits):
        def fetch(url):
            requests.append((url, limits))
            data = (
                page
                if url == page_url
                else (make_png((600, 400)) if read_image else b"not an image")
            )
            return FetchResult(url, "text/html" if url == page_url else "image/png", data, "")

        return SimpleNamespace(fetch=fetch)

    original = DoclingConverter._convert_whole

    def convert_whole(self, path, source_format, progress, base_url, ocr_language, **kwargs):
        if source_format == SourceFormat.HTML:
            return original(self, path, source_format, progress, base_url, ocr_language, **kwargs)
        languages.append(ocr_language)
        return ConversionOutput(
            units=[ConvertedUnit(page=1, markdown="Payment amount: 250 TL.")],
        )

    monkeypatch.setattr(worker, "build_services", lambda _settings: services)
    monkeypatch.setattr(worker, "SafeFetcher", safe_fetcher)
    monkeypatch.setattr(DoclingConverter, "_convert_whole", convert_whole)
    assert worker._convert(services.settings, loop=False, poll_seconds=0) == 0

    [source] = services.sources.list_for(key)
    assert source.status == SourceStatus.CONVERTED
    assert [url for url, _limits in requests] == [page_url, image_url]
    assert requests[1][1]["max_bytes"] <= services.settings.max_url_bytes
    assert requests[1][1]["timeout_seconds"] <= services.settings.url_timeout_seconds
    chunks = json.loads(services.blobs.get(chunks_blob(key, source.id)))
    text = "\n".join(chunk["text"] for chunk in chunks)
    assert all(chunk["id"].startswith("S1-") for chunk in chunks)
    assert "Native service information." in text
    status = workspace_client.get("/sources/status").data
    gaps = collect_gaps([], [source], {})
    if read_image:
        assert languages == ["ar"]
        assert "250 TL" in text
        assert "Text read from image using OCR" in text
        assert image_url in text
        assert source.limitations == []
        assert b"Converted with warnings" not in status
        assert gaps == []
    else:
        assert languages == []
        assert any("Could not read content image" in note for note in source.limitations)
        assert b"Converted with warnings" in status
        assert source.limitations[0].encode() in status
        assert len(gaps) == 1 and gaps[0].kind == "conversion"


@pytest.mark.parametrize("with_answers", [False, True])
def test_html_faq_answers_or_warning_reach_chunks_sources_and_review(
    workspace_client, services, make_processor, with_answers
):
    workspace_client.post("/sources/url", data={"url": "https://example.org/faq"})
    key = session_key_of(workspace_client)
    entries = "".join(
        f"<h2>How can families apply for service {index}?</h2>"
        + (
            '<div style="display:none"><p>Visit the office with your identity card.</p></div>'
            if with_answers
            else ""
        )
        for index in range(4)
    )
    page = (
        "<html><head><title>Registration FAQ</title>"
        '<meta name="date" content="2026-10-09"></head><body>' + entries + "</body></html>"
    ).encode()
    fetcher = FakeFetcher(
        FetchResult(
            final_url="https://example.org/faq",
            content_type="text/html",
            data=page,
            last_modified="",
        )
    )
    make_processor(converter=DoclingConverter(), fetcher=fetcher).process_next()

    [source] = services.sources.list_for(key)
    assert source.status == SourceStatus.CONVERTED
    assert source.title == "Registration FAQ"
    assert source.document_date == "2026-10-09"
    chunks = json.loads(services.blobs.get(chunks_blob(key, source.id)))
    assert chunks and all(chunk["id"].startswith("S1-") for chunk in chunks)
    gaps = collect_gaps([], [source], {})
    status = workspace_client.get("/sources/status").data
    if with_answers:
        assert any("Visit the office with your identity card." in chunk["text"] for chunk in chunks)
        assert source.limitations == []
        assert gaps == []
        assert b"Converted with warnings" not in status
    else:
        assert "headings but very little body text" in source.limitations[0]
        assert b"Converted with warnings" in status
        assert source.limitations[0].encode() in status
        assert len(gaps) == 1 and gaps[0].kind == "conversion"
        assert gaps[0].source_reference == source.reference


def test_html_boilerplate_does_not_reach_stored_chunks_token_budget_or_model_sources(
    workspace_client, services, make_processor
):
    workspace_client.post("/sources/url", data={"url": "https://example.org/support"})
    key = session_key_of(workspace_client)
    menu = "<p>Unrelated repeated navigation menu text.</p>" * 1000
    page = (
        "<html><head><title>Financial support</title>"
        '<meta name="date" content="2026-10-14"></head><body>'
        f"<header>{menu}</header><main><article>"
        "<header><p>Help for families</p><h1>Financial support</h1></header>"
        "<p>Apply at the town hall with proof of income.</p>"
        '<div hidden><p>Read <a href="/documents">the document checklist</a>.</p></div>'
        '<div class="c-contact-flyout"><p>Enter your postcode into this widget.</p></div>'
        '<form><label>Your postcode</label><input name="postcode"></form>'
        "</article></main><footer><p>Newsletter subscription</p></footer></body></html>"
    ).encode()
    assert estimate_tokens(page.decode()) > 10000
    fetcher = FakeFetcher(
        FetchResult(
            final_url="https://example.org/support",
            content_type="text/html",
            data=page,
            last_modified="",
        )
    )

    make_processor(converter=DoclingConverter(), fetcher=fetcher).process_next()

    [source] = services.sources.list_for(key)
    assert source.status == SourceStatus.CONVERTED
    assert source.title == "Financial support" and source.document_date == "2026-10-14"
    assert 0 < source.token_count < 100
    assert source.limitations == []
    markdown = services.blobs.get(markdown_blob(key, source.id)).decode()
    chunks = json.loads(services.blobs.get(chunks_blob(key, source.id)))
    materials, index = load_source_material(services.sources.repository, services.blobs, key)
    texts = [
        markdown,
        *(chunk["text"] for chunk in chunks),
        *(chunk.text for material in materials for chunk in material.chunks),
    ]
    assert index and materials
    for text in texts:
        assert "Apply at the town hall with proof of income." in text
        assert "Help for families" in text
        assert "https://example.org/documents" in text
        assert "postcode" not in text
        assert "navigation menu" not in text
        assert "Newsletter" not in text


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
