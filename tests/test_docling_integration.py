"""Real Docling conversions. Slow and downloads models: run with `pytest -m docling`."""

import pytest
from factories import make_docx, make_text_pdf, make_xlsx
from PIL import Image, ImageDraw, ImageFont

from build_a_hia.services.conversion import DoclingConverter
from build_a_hia.services.inspection import SourceFormat

pytestmark = pytest.mark.docling


@pytest.fixture(scope="module")
def converter():
    return DoclingConverter()


def _ignore_progress(_done, _total):
    return None


def test_pdf_pages_keep_their_numbers_across_chunks(converter, tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(make_text_pdf([f"Service point number {page}" for page in range(1, 8)]))

    output = converter.convert(path, SourceFormat.PDF, _ignore_progress)

    assert [unit.page for unit in output.units] == list(range(1, 8))
    assert "Service point number 7" in output.units[6].markdown


def test_scanned_page_is_read_with_ocr(converter, tmp_path):
    image = Image.new("RGB", (1200, 300), "white")
    ImageDraw.Draw(image).text(
        (40, 100), "FREE SHELTER HERE", fill="black", font=ImageFont.load_default(size=80)
    )
    path = tmp_path / "scan.png"
    image.save(path)

    output = converter.convert(path, SourceFormat.PNG, _ignore_progress)

    assert "SHELTER" in " ".join(unit.markdown for unit in output.units).upper()


def test_docx_and_xlsx_are_converted(converter, tmp_path):
    docx = tmp_path / "doc.docx"
    docx.write_bytes(make_docx("Legal aid on Mondays"))
    xlsx = tmp_path / "book.xlsx"
    xlsx.write_bytes(
        make_xlsx(
            {
                "Contacts": [["Name", "Phone"], ["Hotline", "1545"]],
                "Hours": [["Day", "Open"], ["Monday", "9-17"]],
            }
        )
    )

    word = converter.convert(docx, SourceFormat.DOCX, _ignore_progress)
    excel = converter.convert(xlsx, SourceFormat.XLSX, _ignore_progress)

    assert "Legal aid on Mondays" in word.units[0].markdown
    assert [unit.sheet for unit in excel.units] == ["Contacts", "Hours"]
    assert "1545" in excel.units[0].markdown


def test_html_is_converted_without_remote_fetches(converter, tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        "<html><body><h1>Cash assistance</h1><p>Apply at the town hall.</p>"
        '<img src="http://169.254.169.254/latest/meta-data/"></body></html>',
        encoding="utf-8",
    )

    output = converter.convert(path, SourceFormat.HTML, _ignore_progress)

    assert "Apply at the town hall." in output.units[0].markdown


def test_html_with_root_relative_links_is_converted(converter, tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        '<html><body><h1>Food aid</h1><p>Read <a href="/about/press/">more</a>.</p>'
        '<p><a href="../../outside">Up</a></p></body></html>',
        encoding="utf-8",
    )

    linked = converter.convert(
        path, SourceFormat.HTML, _ignore_progress, base_url="https://example.org/help/food/"
    )
    unlinked = converter.convert(path, SourceFormat.HTML, _ignore_progress)

    assert "https://example.org/about/press/" in linked.units[0].markdown
    assert "Food aid" in unlinked.units[0].markdown
