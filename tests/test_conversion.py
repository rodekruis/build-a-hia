from pathlib import Path
from types import SimpleNamespace

import pytest

from build_a_hia.services import conversion
from build_a_hia.services.conversion import (
    DoclingConverter,
    _export_pages,
    calculate_timeout,
    describe_pages,
    page_chunks,
)
from build_a_hia.services.inspection import SourceError


def test_timeout_scales_with_pages_within_bounds():
    assert calculate_timeout(1) == 60
    assert calculate_timeout(5) == 150
    assert calculate_timeout(50) == 600


def test_pdfs_are_split_into_five_page_chunks():
    assert page_chunks(12) == [(1, 5), (6, 10), (11, 12)]
    assert page_chunks(3) == [(1, 3)]


def test_page_lists_are_described_as_ranges():
    assert describe_pages([7, 1, 2, 3, 9, 8]) == "1-3, 7-9"


class FakeDocument:
    def __init__(self, pages: dict[int, str]) -> None:
        self.pages = pages

    def export_to_markdown(self, page_no: int) -> str:
        return self.pages[page_no]


def test_exported_pages_keep_original_numbers():
    assert _export_pages(FakeDocument({6: "a", 7: "b"}), (6, 7)) == {6: "a", 7: "b"}
    assert _export_pages(FakeDocument({1: "a", 2: "b"}), (6, 7)) == {6: "a", 7: "b"}


@pytest.fixture
def scripted(monkeypatch):
    """A converter whose page ranges return scripted text instead of running Docling."""

    calls: list[tuple[tuple[int, int], bool]] = []
    standard: dict[int, str] = {}
    ocr: dict[int, str] = {}
    failing: set[tuple[int, int]] = set()

    def convert_range(self, path, page_range, *, with_ocr):
        calls.append((page_range, with_ocr))
        if not with_ocr and page_range in failing:
            return None
        texts = ocr if with_ocr else standard
        return {page: texts.get(page, "") for page in range(page_range[0], page_range[1] + 1)}

    monkeypatch.setattr(DoclingConverter, "_convert_range", convert_range)
    return SimpleNamespace(calls=calls, standard=standard, ocr=ocr, failing=failing)


def _run(monkeypatch, pages):
    monkeypatch.setattr(conversion, "count_pdf_pages", lambda _path: pages)
    progress: list[tuple[int, int]] = []
    output = DoclingConverter().convert(
        Path("doc.pdf"),
        conversion.SourceFormat.PDF,
        lambda done, total: progress.append((done, total)),
    )
    return output, progress


def test_ocr_runs_only_for_failed_chunks_and_empty_pages(monkeypatch, scripted):
    scripted.standard.update({page: f"text {page}" for page in range(1, 13)})
    del scripted.standard[3]
    scripted.failing.add((6, 10))
    scripted.ocr.update({3: "scanned 3", 6: "ocr 6", 7: "ocr 7", 8: "ocr 8", 9: "ocr 9"})

    output, progress = _run(monkeypatch, 12)

    assert [call for call in scripted.calls if call[1]] == [((3, 3), True), ((6, 10), True)]
    assert [unit.page for unit in output.units] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12]
    assert output.units[2].markdown == "scanned 3"
    assert output.limitations == [
        "No text could be read from page(s) 10. They may be blank or contain only images."
    ]
    assert progress[0] == (0, 12)
    assert progress[-1] == (12, 12)


def test_document_without_any_text_fails(monkeypatch, scripted):
    with pytest.raises(SourceError) as error:
        _run(monkeypatch, 2)

    assert error.value.code == "no_text"


def test_progress_callback_can_cancel(monkeypatch, scripted):
    scripted.standard.update({page: "text" for page in range(1, 11)})
    monkeypatch.setattr(conversion, "count_pdf_pages", lambda _path: 10)

    def cancel(done, _total):
        if done:
            raise conversion.ConversionCancelledError

    with pytest.raises(conversion.ConversionCancelledError):
        DoclingConverter().convert(Path("doc.pdf"), conversion.SourceFormat.PDF, cancel)

    assert len(scripted.calls) == 1
