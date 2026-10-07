"""Split converted text into labeled chunks that the model can cite.

Chunks exist for citation, not retrieval: every request receives all of them.
"""

import math
import re
from dataclasses import asdict, dataclass

MAX_CHUNK_CHARS = 2000
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class ConvertedUnit:
    """Text from one page or sheet of a converted source.

    Attributes:
        page: Page or sheet number, or None when the format has no pages.
        markdown: Converted text.
        sheet: Sheet name for workbooks, otherwise empty.
    """

    page: int | None
    markdown: str
    sheet: str = ""


@dataclass(frozen=True)
class Chunk:
    """A citable piece of a source's text.

    Attributes:
        id: Citation ID such as ``S2-p3-c1``: source number, page (``p``) or sheet (``s``)
            number when known, and chunk number within that page or sheet.
        page: Page or sheet number, or None when the format has no pages.
        sheet: Sheet name for workbooks, otherwise empty.
        text: Chunk text.
        tokens: Estimated number of tokens.
    """

    id: str
    page: int | None
    sheet: str
    text: str
    tokens: int

    @property
    def label(self) -> str:
        """Citation label as shown to the model, e.g. ``[S2-p3-c1]``."""
        return f"[{self.id}]"

    def to_dict(self) -> dict[str, object]:
        """Return the chunk as a JSON-serializable dict."""
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a text as one token per three UTF-8 bytes."""
    # Conservative across scripts; replace with the deployment's tokenizer once selected.
    return math.ceil(len(text.encode("utf-8")) / 3)


def build_chunks(
    source_number: int, units: list[ConvertedUnit], max_chars: int = MAX_CHUNK_CHARS
) -> list[Chunk]:
    """Split converted units into labeled chunks of at most `max_chars` characters.

    Paragraphs are packed together up to the limit; longer paragraphs are split hard.

    Args:
        source_number: Number of the source, used in chunk IDs.
        units: Converted pages, sheets or whole document.
        max_chars: Maximum characters per chunk.

    Returns:
        The chunks in unit order.
    """
    chunks: list[Chunk] = []
    for unit in units:
        if unit.page is None:
            location = ""
        elif unit.sheet:
            location = f"-s{unit.page}"
        else:
            location = f"-p{unit.page}"
        for index, text in enumerate(_pack(unit.markdown, max_chars), start=1):
            chunks.append(
                Chunk(
                    id=f"S{source_number}{location}-c{index}",
                    page=unit.page,
                    sheet=unit.sheet,
                    text=text,
                    tokens=estimate_tokens(text),
                )
            )
    return chunks


def _pack(markdown: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(markdown):
        paragraph = paragraph.strip()
        while len(paragraph) > max_chars:
            pieces.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:].strip()
        if paragraph:
            pieces.append(paragraph)

    packed: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            packed.append(current)
            current = piece
    if current:
        packed.append(current)
    return packed
