import io
import zipfile

import pytest
from factories import make_docx, make_png, make_text_pdf, make_xlsx

from build_a_hia.services.html_meta import extract_html_metadata
from build_a_hia.services.inspection import SourceError, SourceFormat, inspect_content


def test_pdf_pages_are_counted():
    inspected = inspect_content(make_text_pdf(["a", "b", "c"]), max_pages=10)

    assert inspected.format == SourceFormat.PDF
    assert inspected.page_count == 3


def test_pdf_page_limit_is_enforced():
    with pytest.raises(SourceError) as error:
        inspect_content(make_text_pdf(["a", "b", "c"]), max_pages=2)

    assert error.value.code == "too_many_pages"


def test_damaged_pdf_is_rejected():
    with pytest.raises(SourceError) as error:
        inspect_content(b"%PDF-1.4\nnot really a pdf", max_pages=10)

    assert error.value.code == "malformed"


def test_office_formats_are_detected():
    assert inspect_content(make_docx("Hi"), max_pages=10).format == SourceFormat.DOCX
    xlsx = make_xlsx({"Sheet": [["a"]]})
    assert inspect_content(xlsx, max_pages=10).format == SourceFormat.XLSX


def test_other_zip_files_are_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "hello")

    with pytest.raises(SourceError) as error:
        inspect_content(buffer.getvalue(), max_pages=10)

    assert error.value.code == "unsupported"


def test_encrypted_ooxml_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("EncryptedPackage", "x")

    with pytest.raises(SourceError) as error:
        inspect_content(buffer.getvalue(), max_pages=10)

    assert error.value.code == "encrypted"


def test_images_are_verified():
    assert inspect_content(make_png(), max_pages=10).format == SourceFormat.PNG

    with pytest.raises(SourceError):
        inspect_content(b"\x89PNG\r\n\x1a\n" + b"broken", max_pages=10)


def test_html_is_only_accepted_from_fetches():
    page = b"<html><head><title>Help</title></head><body>Hi</body></html>"

    with pytest.raises(SourceError):
        inspect_content(page, max_pages=10)

    inspected = inspect_content(
        page, max_pages=10, content_type="text/html; charset=utf-8", allow_html=True
    )
    assert inspected.format == SourceFormat.HTML
    assert inspected.title == "Help"


def test_html_metadata_prefers_modified_date():
    metadata = extract_html_metadata(
        b"<title> Cash\n assistance </title>"
        b'<meta property="article:published_time" content="2025-01-02T10:00:00Z">'
        b'<meta property="article:modified_time" content="2026-03-04">'
    )

    assert metadata.title == "Cash assistance"
    assert metadata.date == "2026-03-04"


def test_empty_file_is_rejected():
    with pytest.raises(SourceError) as error:
        inspect_content(b"", max_pages=10)

    assert error.value.code == "empty"
