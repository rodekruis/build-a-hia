"""Validate source content by inspecting bytes, not file names or declared types."""

import io
import re
import zipfile
from dataclasses import dataclass
from enum import StrEnum

from .html_meta import extract_html_metadata

MAX_ZIP_ENTRIES = 5000
MAX_ZIP_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000

_CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_DATE = re.compile(r"^D:(\d{4})(\d{2})(\d{2})")


class SourceFormat(StrEnum):
    """Supported source formats, as detected from content."""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    BMP = "bmp"
    WEBP = "webp"
    HTML = "html"


IMAGE_FORMATS = frozenset(
    {SourceFormat.PNG, SourceFormat.JPEG, SourceFormat.TIFF, SourceFormat.BMP, SourceFormat.WEBP}
)

FILE_EXTENSIONS = {
    SourceFormat.PDF: ".pdf",
    SourceFormat.DOCX: ".docx",
    SourceFormat.XLSX: ".xlsx",
    SourceFormat.PNG: ".png",
    SourceFormat.JPEG: ".jpg",
    SourceFormat.TIFF: ".tiff",
    SourceFormat.BMP: ".bmp",
    SourceFormat.WEBP: ".webp",
    SourceFormat.HTML: ".html",
}

FORMAT_LABELS = {
    SourceFormat.PDF: "PDF",
    SourceFormat.DOCX: "Word document",
    SourceFormat.XLSX: "Excel workbook",
    SourceFormat.HTML: "Web page",
}

_PILLOW_FORMATS = {
    "PNG": SourceFormat.PNG,
    "JPEG": SourceFormat.JPEG,
    "TIFF": SourceFormat.TIFF,
    "BMP": SourceFormat.BMP,
    "WEBP": SourceFormat.WEBP,
}


class SourceError(Exception):
    """A user-facing problem with a source.

    Messages must not include source content.

    Args:
        code: Stable, machine-readable error code.
        message: User-facing explanation.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InspectedContent:
    """Format and metadata detected from source bytes.

    Attributes:
        format: Detected format.
        page_count: Pages of a PDF, 1 for images and 0 for formats without pages.
        title: Title from the document metadata, or empty.
        document_date: ISO date from the document metadata, or empty.
    """

    format: SourceFormat
    page_count: int
    title: str = ""
    document_date: str = ""


def format_label(source_format: str) -> str:
    """Return a user-facing label for a format value, or an empty string if it is unknown."""
    try:
        parsed = SourceFormat(source_format)
    except ValueError:
        return ""
    if parsed in IMAGE_FORMATS:
        return "Image"
    return FORMAT_LABELS.get(parsed, parsed.value.upper())


def inspect_content(
    data: bytes, *, max_pages: int, content_type: str = "", allow_html: bool = False
) -> InspectedContent:
    """Detect and validate the format of source bytes.

    The format is taken from signatures in the content. Zip archives are checked against
    entry-count and expanded-size limits, images against a pixel limit, and PDFs against the
    page limit. Title and date are read from metadata where available.

    Args:
        data: Content to inspect.
        max_pages: Maximum number of pages of a PDF.
        content_type: Declared HTTP content type; only used to recognize HTML.
        allow_html: Accept HTML pages, for downloaded web pages.

    Returns:
        The detected format and metadata.

    Raises:
        SourceError: ``empty``, ``encrypted``, ``encrypted_or_legacy``, ``malformed``,
            ``too_large``, ``too_many_pages`` or ``unsupported``.
    """
    if not data:
        raise SourceError("empty", "The file is empty.")
    if data.startswith(_CFB_SIGNATURE):
        raise SourceError(
            "encrypted_or_legacy",
            "This file is password-protected or uses an old Office format (.doc or .xls). "
            "Remove the password or save it as PDF, DOCX or XLSX, then add it again.",
        )
    if b"%PDF-" in data[:1024]:
        return _inspect_pdf(data, max_pages)
    if data.startswith(b"PK\x03\x04"):
        return _inspect_office(data)
    if _is_image(data):
        return _inspect_image(data)
    if allow_html and _looks_like_html(data, content_type):
        metadata = extract_html_metadata(data)
        return InspectedContent(
            format=SourceFormat.HTML,
            page_count=0,
            title=metadata.title,
            document_date=metadata.date,
        )
    raise SourceError(
        "unsupported",
        "This file type is not supported. Use PDF, DOCX, XLSX or an image "
        "(PNG, JPEG, TIFF, BMP or WebP).",
    )


def _inspect_pdf(data: bytes, max_pages: int) -> InspectedContent:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as error:
        if "password" in str(error).lower():
            raise SourceError(
                "encrypted",
                "This PDF is password-protected. Remove the password and add it again.",
            ) from error
        raise SourceError(
            "malformed", "This PDF could not be opened. It may be damaged."
        ) from error
    try:
        page_count = len(document)
        metadata = document.get_metadata_dict(skip_empty=True)
    finally:
        document.close()
    if page_count == 0:
        raise SourceError("malformed", "This PDF has no pages.")
    if page_count > max_pages:
        raise SourceError(
            "too_many_pages",
            f"This PDF has {page_count} pages; the limit is {max_pages} pages per document.",
        )
    return InspectedContent(
        format=SourceFormat.PDF,
        page_count=page_count,
        title=metadata.get("Title", "").strip()[:200],
        document_date=_pdf_date(metadata.get("ModDate") or metadata.get("CreationDate") or ""),
    )


def _pdf_date(raw: str) -> str:
    match = _PDF_DATE.match(raw)
    if not match:
        return ""
    year, month, day = match.groups()
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return ""
    return f"{year}-{month}-{day}"


def _inspect_office(data: bytes) -> InspectedContent:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise SourceError("malformed", "This file has too many internal parts.")
            if sum(entry.file_size for entry in entries) > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise SourceError("too_large", "This file expands to more data than allowed.")
            names = {entry.filename for entry in entries}
    except zipfile.BadZipFile as error:
        raise SourceError(
            "malformed", "This file could not be opened. It may be damaged."
        ) from error

    if "EncryptedPackage" in names:
        raise SourceError(
            "encrypted", "This file is password-protected. Remove the password and add it again."
        )
    if "[Content_Types].xml" in names:
        if "word/document.xml" in names:
            return InspectedContent(format=SourceFormat.DOCX, page_count=0)
        if "xl/workbook.xml" in names:
            return InspectedContent(format=SourceFormat.XLSX, page_count=0)
    raise SourceError(
        "unsupported", "This file type is not supported. Use PDF, DOCX, XLSX or an image."
    )


def _is_image(data: bytes) -> bool:
    signatures = (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"II*\x00",
        b"MM\x00*",
        b"BM",
    )
    return data.startswith(signatures) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def _inspect_image(data: bytes) -> InspectedContent:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image_format = _PILLOW_FORMATS.get(image.format or "")
            if width * height > MAX_IMAGE_PIXELS:
                raise SourceError("too_large", "This image is too large to process.")
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise SourceError(
            "malformed", "This image could not be opened. It may be damaged."
        ) from error
    if image_format is None:
        raise SourceError("unsupported", "This image type is not supported.")
    return InspectedContent(format=image_format, page_count=1)


def _looks_like_html(data: bytes, content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in ("text/html", "application/xhtml+xml"):
        return False
    return b"\x00" not in data[:4096]
