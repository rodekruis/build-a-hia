"""Convert sources to markdown with Docling on CPU.

Follows the approach used in rodekruis/appeals-monitor: Docling is imported lazily, PDF
pages are counted with pypdfium2, PDFs are converted in small page ranges with page-based
timeouts, and full-page OCR runs only for pages the standard pipeline could not read.
"""

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .chunking import ConvertedUnit
from .inspection import IMAGE_FORMATS, SourceError, SourceFormat

logger = logging.getLogger(__name__)

TIMEOUT_PER_PAGE = 30.0
MIN_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0
# Small page ranges avoid native out-of-memory errors (std::bad_alloc) on large PDFs.
PDF_CHUNK_PAGES = 5

ProgressCallback = Callable[[int, int], None]


class ConversionCancelledError(Exception):
    """Raised by a progress callback to stop a conversion that is no longer wanted."""


@dataclass
class ConversionOutput:
    """Converted text of a source.

    Attributes:
        units: Pages, sheets or the whole document, limited to units with readable text.
        limitations: User-facing notes on content that could not be converted.
    """

    units: list[ConvertedUnit]
    limitations: list[str] = field(default_factory=list)


def calculate_timeout(num_pages: int) -> float:
    """Return the conversion timeout in seconds for a number of pages, within fixed bounds."""
    return min(max(num_pages * TIMEOUT_PER_PAGE, MIN_TIMEOUT), MAX_TIMEOUT)


def page_chunks(num_pages: int, size: int = PDF_CHUNK_PAGES) -> list[tuple[int, int]]:
    """Split pages 1 to `num_pages` into inclusive ranges of at most `size` pages."""
    return [(start, min(start + size - 1, num_pages)) for start in range(1, num_pages + 1, size)]


def page_ranges(pages: list[int]) -> list[tuple[int, int]]:
    """Group page numbers into sorted, inclusive ranges of consecutive pages."""
    ranges: list[tuple[int, int]] = []
    for page in sorted(pages):
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], page)
        else:
            ranges.append((page, page))
    return ranges


def describe_pages(pages: list[int]) -> str:
    """Format page numbers for users, e.g. ``1-3, 7``."""
    return ", ".join(
        str(start) if start == end else f"{start}-{end}" for start, end in page_ranges(pages)
    )


def count_pdf_pages(path: Path) -> int:
    """Return the number of pages of a PDF file."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(path)
    try:
        return len(document)
    finally:
        document.close()


@cache
def _docling() -> SimpleNamespace:
    from docling.datamodel.backend_options import HTMLBackendOptions
    from docling.datamodel.base_models import ConversionStatus, DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import OcrAutoOptions, OcrMode, PdfPipelineOptions
    from docling.document_converter import (
        DocumentConverter,
        HTMLFormatOption,
        ImageFormatOption,
        PdfFormatOption,
    )

    return SimpleNamespace(
        ConversionStatus=ConversionStatus,
        DocumentConverter=DocumentConverter,
        DocumentStream=DocumentStream,
        HTMLBackendOptions=HTMLBackendOptions,
        HTMLFormatOption=HTMLFormatOption,
        ImageFormatOption=ImageFormatOption,
        InputFormat=InputFormat,
        OcrAutoOptions=OcrAutoOptions,
        OcrMode=OcrMode,
        PdfFormatOption=PdfFormatOption,
        PdfPipelineOptions=PdfPipelineOptions,
    )


class DoclingConverter:
    """Convert source files to markdown with Docling, reusing converters across calls."""

    def __init__(self) -> None:
        # Converters cache their loaded models, so reuse them across chunks.
        self._converters: dict[tuple[bool, float, bool], Any] = {}

    def convert(
        self,
        path: Path,
        source_format: SourceFormat,
        progress: ProgressCallback,
        *,
        base_url: str = "",
    ) -> ConversionOutput:
        """Convert a file into units of markdown.

        PDFs are converted in small page ranges, and pages without text are retried with
        full-page OCR. Other formats are converted as a whole; images always use OCR.

        Args:
            path: File to convert.
            source_format: Format detected by inspection.
            progress: Called with pages done and total pages; may raise to stop the conversion.
            base_url: Address of a fetched web page, used to make its relative links absolute.
                Nothing is fetched from it.

        Returns:
            The units with readable text and any limitations.

        Raises:
            SourceError: ``no_text`` if no readable text was found, or ``conversion_failed``
                if a non-PDF file could not be converted.
        """
        if source_format == SourceFormat.PDF:
            return self._convert_pdf(path, progress)
        return self._convert_whole(path, source_format, progress, base_url)

    def prefetch_models(self) -> None:
        """Download and load the PDF and image pipeline models before the first conversion."""
        docling = _docling()
        for with_ocr in (False, True):
            converter = self._converter(paginated=True, timeout=MIN_TIMEOUT, with_ocr=with_ocr)
            converter.initialize_pipeline(docling.InputFormat.PDF)
        self._converter(paginated=True, timeout=MAX_TIMEOUT, with_ocr=True).initialize_pipeline(
            docling.InputFormat.IMAGE
        )

    def _converter(self, *, paginated: bool, timeout: float, with_ocr: bool) -> Any:
        key = (paginated, timeout, with_ocr)
        if key in self._converters:
            return self._converters[key]
        docling = _docling()
        if paginated:
            options = docling.PdfPipelineOptions(document_timeout=timeout, do_ocr=with_ocr)
            if with_ocr:
                options.ocr_options = docling.OcrAutoOptions(mode=docling.OcrMode.FULL_PAGE)
            converter = docling.DocumentConverter(
                allowed_formats=[docling.InputFormat.PDF, docling.InputFormat.IMAGE],
                format_options={
                    docling.InputFormat.PDF: docling.PdfFormatOption(pipeline_options=options),
                    docling.InputFormat.IMAGE: docling.ImageFormatOption(pipeline_options=options),
                },
            )
        else:
            converter = docling.DocumentConverter(
                allowed_formats=[docling.InputFormat.DOCX, docling.InputFormat.XLSX],
            )
        self._converters[key] = converter
        return converter

    @staticmethod
    def _html_converter(base_url: str) -> Any:
        docling = _docling()
        html_options = docling.HTMLBackendOptions(
            enable_remote_fetch=False,
            enable_local_fetch=False,
            fetch_images=False,
            render_page=False,
            source_uri=base_url or None,
        )
        return docling.DocumentConverter(
            allowed_formats=[docling.InputFormat.HTML],
            format_options={
                docling.InputFormat.HTML: docling.HTMLFormatOption(backend_options=html_options),
            },
        )

    def _convert_pdf(self, path: Path, progress: ProgressCallback) -> ConversionOutput:
        total = count_pdf_pages(path)
        progress(0, total)
        texts: dict[int, str] = {}
        retry: list[tuple[int, int]] = []
        done = 0

        for start, end in page_chunks(total):
            converted = self._convert_range(path, (start, end), with_ocr=False)
            if converted is None:
                retry.append((start, end))
            else:
                texts.update({page: text for page, text in converted.items() if text.strip()})
                empty = [page for page in range(start, end + 1) if page not in texts]
                retry.extend(page_ranges(empty))
                done += end - start + 1 - len(empty)
            progress(done, total)

        missing: list[int] = []
        for start, end in retry:
            converted = self._convert_range(path, (start, end), with_ocr=True) or {}
            for page in range(start, end + 1):
                text = converted.get(page, "")
                if text.strip():
                    texts[page] = text
                else:
                    missing.append(page)
            done += end - start + 1
            progress(done, total)

        if not texts:
            raise SourceError("no_text", "No readable text was found in this document.")
        limitations = []
        if missing:
            limitations.append(
                f"No text could be read from page(s) {describe_pages(missing)}. "
                "They may be blank or contain only images."
            )
        units = [ConvertedUnit(page=page, markdown=texts[page]) for page in sorted(texts)]
        return ConversionOutput(units=units, limitations=limitations)

    def _convert_range(
        self, path: Path, page_range: tuple[int, int], *, with_ocr: bool
    ) -> dict[int, str] | None:
        docling = _docling()
        timeout = calculate_timeout(page_range[1] - page_range[0] + 1)
        converter = self._converter(paginated=True, timeout=timeout, with_ocr=with_ocr)
        try:
            result = converter.convert(path, raises_on_error=False, page_range=page_range)
        except Exception as error:
            logger.warning(
                "PDF conversion failed for pages %d-%d (ocr=%s): %s",
                *page_range,
                with_ocr,
                type(error).__name__,
            )
            return None
        if result.status not in (
            docling.ConversionStatus.SUCCESS,
            docling.ConversionStatus.PARTIAL_SUCCESS,
        ):
            logger.warning(
                "PDF conversion failed for pages %d-%d (ocr=%s): status %s",
                *page_range,
                with_ocr,
                result.status.value,
            )
            return None
        return _export_pages(result.document, page_range)

    def _convert_whole(
        self, path: Path, source_format: SourceFormat, progress: ProgressCallback, base_url: str
    ) -> ConversionOutput:
        docling = _docling()
        progress(0, 1)
        is_image = source_format in IMAGE_FORMATS
        failed = SourceError("conversion_failed", "This file could not be converted.")
        source: Any = path
        if source_format == SourceFormat.HTML:
            converter = self._html_converter(base_url)
            # From a file path, Docling resolves links like "/about/" against the temp folder
            # and fails the whole page; a stream uses `base_url`, or keeps links as written.
            source = docling.DocumentStream(name=path.name, stream=io.BytesIO(path.read_bytes()))
        else:
            converter = self._converter(paginated=is_image, timeout=MAX_TIMEOUT, with_ocr=is_image)
        try:
            result = converter.convert(source, raises_on_error=False)
        except Exception as error:
            logger.warning("Conversion failed: %s", type(error).__name__)
            raise failed from error
        if result.status not in (
            docling.ConversionStatus.SUCCESS,
            docling.ConversionStatus.PARTIAL_SUCCESS,
        ):
            logger.warning(
                "Conversion failed: status %s, %d error(s)", result.status.value, len(result.errors)
            )
            raise failed

        document = result.document
        sheet_names = _sheet_names(path) if source_format == SourceFormat.XLSX else []
        if document.pages:
            numbers = sorted(document.pages)
            use_sheets = len(sheet_names) == len(numbers)
            units = [
                ConvertedUnit(
                    page=number,
                    markdown=document.export_to_markdown(page_no=number),
                    sheet=sheet_names[index] if use_sheets else "",
                )
                for index, number in enumerate(numbers)
            ]
        else:
            units = [ConvertedUnit(page=None, markdown=document.export_to_markdown())]
        units = [unit for unit in units if unit.markdown.strip()]
        if not units:
            if source_format == SourceFormat.HTML:
                raise SourceError(
                    "no_text",
                    "The page has little or no readable text. It may need JavaScript to show "
                    "its content; upload the document or save the page as PDF instead.",
                )
            raise SourceError("no_text", "No readable text was found in this file.")
        limitations = []
        if result.status == docling.ConversionStatus.PARTIAL_SUCCESS:
            limitations.append("Part of this file could not be converted.")
        progress(1, 1)
        return ConversionOutput(units=units, limitations=limitations)


def _export_pages(document: Any, page_range: tuple[int, int]) -> dict[int, str]:
    start, end = page_range
    numbers = sorted(document.pages)
    # Keep original page numbers even if Docling numbered the range from 1.
    offset = 0 if all(start <= number <= end for number in numbers) else start - 1
    return {number + offset: document.export_to_markdown(page_no=number) for number in numbers}


def _sheet_names(path: Path) -> list[str]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(path, read_only=True)
    except Exception:
        return []
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()
