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
    partial: set[tuple[int, int]] = set()
    ocr_failing: set[tuple[int, int]] = set()

    def convert_range(self, path, page_range, *, with_ocr, ocr_language="en"):
        calls.append((page_range, with_ocr))
        if page_range in (ocr_failing if with_ocr else failing):
            return None
        texts = ocr if with_ocr else standard
        return conversion._PageConversion(
            texts={page: texts.get(page, "") for page in range(page_range[0], page_range[1] + 1)},
            partial=page_range in partial,
        )

    monkeypatch.setattr(DoclingConverter, "_convert_range", convert_range)
    return SimpleNamespace(
        calls=calls,
        standard=standard,
        ocr=ocr,
        failing=failing,
        partial=partial,
        ocr_failing=ocr_failing,
    )


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
    scripted.standard.update(
        {page: f"Readable service information for page {page}. " * 5 for page in range(1, 13)}
    )
    del scripted.standard[3]
    scripted.failing.add((6, 10))
    scripted.ocr.update({3: "scanned 3", 6: "ocr 6", 7: "ocr 7", 8: "ocr 8", 9: "ocr 9"})

    output, progress = _run(monkeypatch, 12)

    assert [call for call in scripted.calls if call[1]] == [((3, 3), True), ((6, 10), True)]
    assert [unit.page for unit in output.units] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12]
    assert output.units[2].markdown == "scanned 3"
    assert any("page(s) 10" in limitation for limitation in output.limitations)
    assert progress[0] == (0, 12)
    assert progress[-1] == (12, 12)


def test_document_without_any_text_fails(monkeypatch, scripted):
    with pytest.raises(SourceError) as error:
        _run(monkeypatch, 2)

    assert error.value.code == "no_text"


def test_progress_callback_can_cancel(monkeypatch, scripted):
    scripted.standard.update({page: "Readable native text. " * 10 for page in range(1, 11)})
    monkeypatch.setattr(conversion, "count_pdf_pages", lambda _path: 10)

    def cancel(done, _total):
        if done:
            raise conversion.ConversionCancelledError

    with pytest.raises(conversion.ConversionCancelledError):
        DoclingConverter().convert(Path("doc.pdf"), conversion.SourceFormat.PDF, cancel)

    assert len(scripted.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "<!-- image -->",
        "<!-- image -->\n\n<!-- image -->",
        "![](data:image/png;base64,abc)",
        "# | --- <!-- image -->",
    ],
)
def test_placeholder_only_pages_are_retried_then_rejected(monkeypatch, scripted, text):
    scripted.standard[1] = scripted.ocr[1] = text

    with pytest.raises(SourceError, match="No readable text"):
        _run(monkeypatch, 1)

    assert scripted.calls == [((1, 1), False), ((1, 1), True)]


def test_placeholder_page_recovers_text_with_ocr(monkeypatch, scripted):
    scripted.standard[1] = "<!-- image -->"
    scripted.ocr[1] = "Shelter registration is open every Monday. " * 5

    output, progress = _run(monkeypatch, 1)

    assert output.units[0].markdown == scripted.ocr[1]
    assert output.limitations == []
    assert progress[-1] == (1, 1)


def test_sparse_pages_keep_native_text_when_ocr_is_worse(monkeypatch, scripted):
    scripted.standard[1] = "Call 112."
    scripted.ocr[1] = "<!-- image -->"

    output, _ = _run(monkeypatch, 1)

    assert output.units[0].markdown == "Call 112."
    assert any("Very little text" in note and "1" in note for note in output.limitations)


@pytest.mark.parametrize(("length", "retried"), [(87, True), (88, False), (90, False)])
def test_low_text_threshold_is_per_page(monkeypatch, scripted, length, retried):
    scripted.standard[1] = "a" * length
    scripted.standard[2] = "Good native text. " * 100
    scripted.ocr[1] = scripted.standard[1]

    output, _ = _run(monkeypatch, 2)

    assert (((1, 1), True) in scripted.calls) is retried
    assert bool(output.limitations) is retried


def test_readable_text_preserves_unicode_numbers_and_links():
    markdown = "# Hotline\n<!-- image -->\n[Call 112](https://example.org)\n| مرحبا | **Help** |"
    text = conversion.readable_text(markdown)

    assert "image" not in text
    assert "https://" not in text
    assert "مرحبا" in text and "112" in text and "Help" in text


def test_partial_pdf_result_is_not_silently_accepted(monkeypatch, scripted):
    scripted.standard[1] = "Useful native content. " * 20
    scripted.partial.add((1, 1))
    output, _ = _run(monkeypatch, 1)
    assert any("partial results" in note for note in output.limitations)


def test_ocr_initialization_failure_fails_unreadable_document(monkeypatch, tmp_path):
    monkeypatch.setattr(conversion, "count_pdf_pages", lambda _path: 1)
    monkeypatch.setattr(conversion, "_docling", lambda: SimpleNamespace())
    converter = DoclingConverter()

    def fail(**kwargs):
        raise RuntimeError("OCR model unavailable")

    monkeypatch.setattr(converter, "_converter", fail)
    with pytest.raises(SourceError, match="OCR could not complete"):
        converter.convert(tmp_path / "scan.pdf", conversion.SourceFormat.PDF, lambda *_args: None)


def test_ocr_failure_preserves_native_text_with_warning(monkeypatch, scripted):
    scripted.standard[1] = "Call 112."
    scripted.ocr_failing.add((1, 1))
    output, _ = _run(monkeypatch, 1)
    assert output.units[0].markdown == "Call 112."
    assert any("OCR could not complete" in note for note in output.limitations)


def test_ocr_cache_and_prefetch_cover_each_distinct_language_model(monkeypatch):
    models = []
    initialized = []
    docling = SimpleNamespace(
        OcrMode=SimpleNamespace(FULL_PAGE="full"),
        InputFormat=SimpleNamespace(PDF="pdf", IMAGE="image"),
        PdfPipelineOptions=lambda **kwargs: SimpleNamespace(**kwargs),
        RapidOcrOptions=lambda **kwargs: SimpleNamespace(**kwargs),
        PdfFormatOption=lambda **kwargs: SimpleNamespace(**kwargs),
        ImageFormatOption=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    def create(**kwargs):
        options = kwargs["format_options"]["pdf"].pipeline_options
        if options.do_ocr:
            models.append(options.ocr_options.lang[0])
        return SimpleNamespace(initialize_pipeline=initialized.append)

    docling.DocumentConverter = create
    monkeypatch.setattr(conversion, "_docling", lambda: docling)
    converter = DoclingConverter()
    arabic = converter._converter(paginated=True, timeout=60, with_ocr=True, ocr_language="ar")
    assert models == ["arabic"]
    assert (
        converter._converter(paginated=True, timeout=60, with_ocr=True, ocr_language="ar") is arabic
    )
    converter._converters.clear()
    models.clear()
    converter.prefetch_models()
    assert set(models) == set(conversion.OCR_LANGUAGES.values())
    assert initialized.count("image") == len(set(conversion.OCR_LANGUAGES.values()))


@pytest.mark.parametrize(
    "source_format", [conversion.SourceFormat.PNG, conversion.SourceFormat.HTML]
)
def test_placeholder_only_whole_document_fails(monkeypatch, source_format, tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"unused")
    document = SimpleNamespace(pages={}, export_to_markdown=lambda: "<!-- image -->")
    result = SimpleNamespace(status="success", document=document)
    converter = SimpleNamespace(convert=lambda *args, **kwargs: result)
    monkeypatch.setattr(
        conversion,
        "_docling",
        lambda: SimpleNamespace(
            ConversionStatus=SimpleNamespace(SUCCESS="success", PARTIAL_SUCCESS="partial"),
            DocumentStream=lambda **kwargs: kwargs,
        ),
    )
    monkeypatch.setattr(DoclingConverter, "_converter", lambda *args, **kwargs: converter)
    monkeypatch.setattr(DoclingConverter, "_html_converter", lambda *args: converter)

    with pytest.raises(SourceError) as error:
        DoclingConverter().convert(path, source_format, lambda *_args: None)

    assert error.value.code == "no_text"
