import io
import json

import pytest
from factories import CONTEXT_FORM, make_docx, make_png, session_key_of, upload

from build_a_hia import create_app
from build_a_hia.config import TestConfig
from build_a_hia.services.review import collect_gaps
from build_a_hia.services.sources import Source, SourceStatus, original_blob
from build_a_hia.services.storage import StoredEntity


def _sources(services, client):
    return services.sources.list_for(session_key_of(client))


def test_uploading_pdf_queues_conversion(workspace_client, services, pdf_bytes):
    response = upload(workspace_client, "shelter.pdf", pdf_bytes)

    assert response.status_code == 302
    [source] = _sources(services, workspace_client)
    assert source.status == SourceStatus.QUEUED
    assert source.reference == "S1"
    assert source.format == "pdf"
    assert source.page_count == 2
    assert source.title == "shelter.pdf"
    key = session_key_of(workspace_client)
    assert services.blobs.get(original_blob(key, source.id)) == pdf_bytes
    message = services.queue.receive(60)
    assert message.payload == {"session": key, "source": source.id, "version": 1}


def test_multiple_files_get_sequential_references(workspace_client, services, pdf_bytes):
    workspace_client.post(
        "/sources/files",
        data={
            "files": [
                (io.BytesIO(pdf_bytes), "a.pdf"),
                (io.BytesIO(make_docx("Hello")), "b.docx"),
            ]
        },
        content_type="multipart/form-data",
    )

    sources = _sources(services, workspace_client)
    assert [(source.reference, source.format) for source in sources] == [
        ("S1", "pdf"),
        ("S2", "docx"),
    ]


def test_file_type_is_detected_from_content(workspace_client, services):
    upload(workspace_client, "photo.pdf", make_png())

    [source] = _sources(services, workspace_client)
    assert source.format == "png"


def test_unsupported_content_is_rejected(workspace_client, services):
    response = upload(workspace_client, "notes.pdf", b"just some text")

    assert response.status_code == 302
    assert _sources(services, workspace_client) == []
    page = workspace_client.get("/sources")
    assert b"not supported" in page.data


def test_password_protected_office_file_is_rejected(workspace_client, services):
    upload(workspace_client, "secret.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)

    assert _sources(services, workspace_client) == []
    assert b"password-protected" in workspace_client.get("/sources").data


def test_source_count_is_limited(storage_dir, pdf_bytes):
    class Config(TestConfig):
        HIA_LOCAL_STORAGE_DIR = str(storage_dir)
        HIA_MAX_SOURCES = 1

    limited_app = create_app(Config)
    client = limited_app.test_client()
    client.post("/context", data=CONTEXT_FORM)
    upload(client, "a.pdf", pdf_bytes)
    upload(client, "b.pdf", pdf_bytes)

    services = limited_app.extensions["hia_services"]
    assert len(_sources(services, client)) == 1
    assert b"at most 1 sources" in client.get("/sources").data


def test_adding_url_queues_fetch(workspace_client, services):
    response = workspace_client.post(
        "/sources/url",
        data={"url": "https://example.org/help?lang=uk#top"},
    )

    assert response.status_code == 302
    [source] = _sources(services, workspace_client)
    assert source.url == "https://example.org/help?lang=uk"
    assert source.title == ""
    assert source.status == SourceStatus.QUEUED
    assert b'name="title"' not in workspace_client.get("/sources").data


def test_private_url_is_rejected(workspace_client, services):
    response = workspace_client.post("/sources/url", data={"url": "http://169.254.169.254/"})

    assert response.status_code == 400
    assert b"Only public websites" in response.data
    assert _sources(services, workspace_client) == []


def test_non_http_url_is_rejected(workspace_client, services):
    response = workspace_client.post("/sources/url", data={"url": "file:///etc/passwd"})

    assert response.status_code == 400
    assert _sources(services, workspace_client) == []


def test_sources_are_isolated_between_sessions(workspace_client, app, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    other = app.test_client()
    other.post("/context", data={**CONTEXT_FORM, "country": "Moldova"})

    assert other.get(f"/sources/{source.id}").status_code == 404
    assert other.post(f"/sources/{source.id}/remove").status_code == 404
    assert len(_sources(services, workspace_client)) == 1


def test_invalid_source_id_returns_not_found(workspace_client):
    assert workspace_client.get("/sources/../../etc").status_code == 404
    assert workspace_client.get("/sources/abc").status_code == 404


def test_cancel_retry_and_remove(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    key = session_key_of(workspace_client)
    [source] = _sources(services, workspace_client)

    workspace_client.post(f"/sources/{source.id}/cancel")
    cancelled = services.sources.get(key, source.id)
    assert cancelled.status == SourceStatus.CANCELLED
    assert cancelled.version == 2

    workspace_client.post(f"/sources/{source.id}/retry")
    retried = services.sources.get(key, source.id)
    assert retried.status == SourceStatus.QUEUED
    assert retried.version == 3

    workspace_client.post(f"/sources/{source.id}/remove")
    assert services.sources.get(key, source.id) is None
    assert services.blobs.list_children(f"sessions/{key}/sources/") == set()


def test_source_details_can_be_edited(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)

    response = workspace_client.post(
        f"/sources/{source.id}", data={"title": "Shelter guide", "document_date": "2026-09-30"}
    )

    assert response.status_code == 302
    updated = services.sources.get(session_key_of(workspace_client), source.id)
    assert updated.title == "Shelter guide"
    assert updated.document_date == "2026-09-30"
    assert updated.document_date_origin == "user"


def _mark(services, client, source_id, **changes):
    def apply(source):
        for name, value in changes.items():
            setattr(source, name, value)
        return True

    services.sources.repository.mutate(session_key_of(client), source_id, apply)


@pytest.mark.parametrize("endpoint", ["/sources", "/sources/status", "detail"])
def test_converted_sources_with_limitations_show_warning_badge(
    workspace_client, services, pdf_bytes, endpoint
):
    upload(workspace_client, "scan.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    _mark(
        services,
        workspace_client,
        source.id,
        status=SourceStatus.CONVERTED,
        limitations=["Very little text could be read from page(s) 1."],
    )

    response = workspace_client.get(f"/sources/{source.id}" if endpoint == "detail" else endpoint)

    assert b"Converted with warnings" in response.data
    assert b"badge-warning" in response.data
    assert b"Very little text" in response.data


@pytest.mark.parametrize("endpoint", ["/sources", "/sources/status", "detail"])
@pytest.mark.parametrize("with_other_warning", [False, True])
def test_stored_web_image_ocr_warnings_are_removed_everywhere(
    workspace_client, services, endpoint, with_other_warning
):
    workspace_client.post("/sources/url", data={"url": "https://example.org/help"})
    [source] = _sources(services, workspace_client)
    retired = [
        "Content image https://example.org/photo.jpg: Very little text could be read "
        "from this image (under 30 estimated tokens per page). "
        "Check the converted text and selected language.",
        "Content image https://example.org/notice.tiff: Part of this file could not be converted.",
        "Could not read content image data:image/svg+xml,%3Csvg%3E: Only http and https "
        "web addresses are supported. Add the image directly as a source or upload a copy.",
        "Could not read content image https://example.org/notice.png: The download timed out. "
        "Add the image directly as a source or upload a copy.",
        "Could not read a content image because its address is invalid. "
        "Upload the image as a separate source.",
        "Text in 1 content image(s) was read using OCR. Compare the images with the converted "
        "text before approving this source: "
        "OCR may omit or misread words, numbers and table entries.",
        "2 content image(s) were not read because the page's image count, "
        "download or processing-time limit was reached. "
        "Add those images directly as sources or upload copies.",
    ]
    other = (
        [
            "This page contains many headings but very little body text. "
            "FAQ answers or other content may be missing."
        ]
        if with_other_warning
        else []
    )
    _mark(
        services,
        workspace_client,
        source.id,
        format="html",
        status=SourceStatus.CONVERTED,
        limitations=retired + other,
    )

    response = workspace_client.get(f"/sources/{source.id}" if endpoint == "detail" else endpoint)
    assert response.status_code == 200
    assert (b"Converted with warnings" in response.data) is with_other_warning
    for fragment in (b"content image", b"Content image", b"data:image/svg+xml", b"Text in 1"):
        assert fragment not in response.data
    assert (b"very little body text" in response.data) is with_other_warning
    [loaded] = _sources(services, workspace_client)
    assert loaded.limitations == other
    conversion_gaps = [gap for gap in collect_gaps([], [loaded], {}) if gap.kind == "conversion"]
    assert len(conversion_gaps) == len(other)


def test_sources_page_warns_that_web_page_images_are_not_read(workspace_client):
    response = workspace_client.get("/sources")

    assert b"Text inside images on web pages" in response.data
    assert b"add the image address as a" in response.data


def test_ocr_language_is_validated_persisted_and_editable_before_retry(
    workspace_client, services, pdf_bytes
):
    response = workspace_client.post(
        "/sources/files",
        data={
            "files": [(io.BytesIO(pdf_bytes), "scan.pdf")],
            "ocr_language": "ar",
        },
    )
    assert response.status_code == 302
    [source] = _sources(services, workspace_client)
    assert source.ocr_language == "ar"
    assert b"Arabic" in workspace_client.get(f"/sources/{source.id}").data
    response = workspace_client.post(f"/sources/{source.id}", data={"title": "Renamed scan"})
    assert response.status_code == 302
    assert _sources(services, workspace_client)[0].ocr_language == "ar"

    response = workspace_client.post(
        f"/sources/{source.id}",
        data={
            "title": "Scan",
            "ocr_language": "uk",
        },
    )
    assert response.status_code == 400
    assert _sources(services, workspace_client)[0].ocr_language == "ar"
    workspace_client.post(f"/sources/{source.id}/cancel")
    response = workspace_client.post(
        f"/sources/{source.id}",
        data={
            "title": "Scan",
            "ocr_language": "uk",
        },
    )
    assert response.status_code == 302
    workspace_client.post(f"/sources/{source.id}/retry")
    assert _sources(services, workspace_client)[0].ocr_language == "uk"

    workspace_client.post(
        "/sources/files",
        data={
            "files": [(io.BytesIO(pdf_bytes), "invalid.pdf")],
            "ocr_language": "invalid",
        },
    )
    assert len(_sources(services, workspace_client)) == 1
    response = workspace_client.post(
        "/sources/url",
        data={
            "url": "https://example.org/scan.pdf",
            "ocr_language": "ar",
        },
    )
    assert response.status_code == 302
    assert _sources(services, workspace_client)[1].ocr_language == "ar"


def test_ocr_language_autocomplete_accepts_a_supported_language_name(
    workspace_client, services, pdf_bytes
):
    page = workspace_client.get("/sources").data
    assert b'type="text"' in page
    assert b'list="ocr-language-options"' in page
    assert b'value="Afrikaans">Afrikaans \xc2\xb7 af' in page
    assert b'value="Korean">Korean \xc2\xb7 ko' in page
    assert b'value="Serbian (Latin)">Serbian (Latin) \xc2\xb7 sr-Latn' in page

    response = workspace_client.post(
        "/sources/files",
        data={
            "files": [(io.BytesIO(pdf_bytes), "scan.pdf")],
            "ocr_language": "Korean",
        },
    )

    assert response.status_code == 302
    [source] = _sources(services, workspace_client)
    assert source.ocr_language == "ko"


@pytest.mark.parametrize("status", [SourceStatus.CONVERTED, SourceStatus.FAILED])
def test_web_page_details_hide_the_unused_scanned_text_language(workspace_client, services, status):
    workspace_client.post(
        "/sources/url", data={"url": "https://example.org/help", "ocr_language": "ar"}
    )
    [source] = _sources(services, workspace_client)
    _mark(services, workspace_client, source.id, format="html", status=status)

    page = workspace_client.get(f"/sources/{source.id}").data
    assert b"Source language:" not in page
    assert b"Language of source" not in page
    response = workspace_client.post(
        f"/sources/{source.id}", data={"title": "Help page", "ocr_language": "ar"}
    )
    assert response.status_code == 302
    [saved] = _sources(services, workspace_client)
    assert saved.title == "Help page"
    assert saved.ocr_language == "ar"


def test_legacy_sources_without_ocr_language_remain_loadable(workspace_client, services, pdf_bytes):
    upload(workspace_client, "native.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    data = source.to_data()
    del data["ocr_language"]
    loaded = Source.from_stored(source.id, StoredEntity(data=data, etag="legacy"))
    assert loaded.ocr_language == "en"
    assert loaded.status == SourceStatus.QUEUED


def test_converted_warning_source_remains_usable(workspace_client, services, pdf_bytes):
    upload(workspace_client, "scan.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    _mark(
        services,
        workspace_client,
        source.id,
        status=SourceStatus.CONVERTED,
        token_count=10,
        limitations=["Very little text."],
    )
    workspace_client.post("/sources/approve")
    assert services.sessions.get_active(session_key_of(workspace_client)).sources_approved


def test_approval_requires_finished_conversion(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)

    workspace_client.post("/sources/approve")

    workspace = services.sessions.get_active(session_key_of(workspace_client))
    assert not workspace.sources_approved


def test_approval_with_failed_source_needs_confirmation(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    upload(workspace_client, "b.pdf", pdf_bytes)
    first, second = _sources(services, workspace_client)
    _mark(services, workspace_client, first.id, status=SourceStatus.CONVERTED, token_count=100)
    _mark(services, workspace_client, second.id, status=SourceStatus.FAILED)

    workspace_client.post("/sources/approve")
    key = session_key_of(workspace_client)
    assert not services.sessions.get_active(key).sources_approved

    workspace_client.post("/sources/approve", data={"accept_failed": "y"})
    assert services.sessions.get_active(key).sources_approved


def test_approval_is_blocked_over_token_limit(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    limit = services.settings.max_session_source_tokens
    _mark(
        services, workspace_client, source.id, status=SourceStatus.CONVERTED, token_count=limit + 1
    )

    workspace_client.post("/sources/approve")

    assert not services.sessions.get_active(session_key_of(workspace_client)).sources_approved
    assert b"too long" in workspace_client.get("/sources").data


def test_adding_a_source_invalidates_approval(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    _mark(services, workspace_client, source.id, status=SourceStatus.CONVERTED, token_count=10)
    workspace_client.post("/sources/approve")
    key = session_key_of(workspace_client)
    assert services.sessions.get_active(key).sources_approved

    upload(workspace_client, "b.pdf", pdf_bytes)

    assert not services.sessions.get_active(key).sources_approved


def test_status_fragment_polls_only_while_work_is_active(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)

    active = workspace_client.get("/sources/status")
    assert b'data-poll-active="true"' in active.data

    [source] = _sources(services, workspace_client)
    _mark(services, workspace_client, source.id, status=SourceStatus.CONVERTED)
    done = workspace_client.get("/sources/status")
    assert b'data-poll-active="false"' in done.data


def test_status_fragment_without_session_is_gone(client):
    assert client.get("/sources/status").status_code == 410


def test_source_preview_shows_converted_text(workspace_client, services, pdf_bytes):
    upload(workspace_client, "a.pdf", pdf_bytes)
    [source] = _sources(services, workspace_client)
    key = session_key_of(workspace_client)
    services.blobs.put(f"sessions/{key}/sources/{source.id}/document.md", b"<b>Shelter</b>")
    services.blobs.put(f"sessions/{key}/sources/{source.id}/chunks.json", json.dumps([]).encode())
    _mark(services, workspace_client, source.id, status=SourceStatus.CONVERTED, chunk_count=1)

    response = workspace_client.get(f"/sources/{source.id}")

    assert b"&lt;b&gt;Shelter&lt;/b&gt;" in response.data
