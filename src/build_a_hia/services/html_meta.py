"""Read title and date metadata from HTML pages without rendering them."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

MAX_PARSE_BYTES = 512 * 1024
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_META = (
    "article:modified_time",
    "og:updated_time",
    "dcterms.modified",
    "last-modified",
    "article:published_time",
    "dcterms.date",
    "date",
)


@dataclass(frozen=True)
class HtmlMetadata:
    """Metadata found in an HTML page.

    Attributes:
        title: Whitespace-normalized ``<title>`` text of at most 200 characters, or empty.
        date: ISO date from the first matching date meta tag, or empty.
    """

    title: str
    date: str


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attributes = {name.lower(): value or "" for name, value in attrs}
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            if name and "content" in attributes:
                self.meta.setdefault(name, attributes["content"].strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def extract_html_metadata(data: bytes) -> HtmlMetadata:
    """Read the title and document date of an HTML page.

    Only the first `MAX_PARSE_BYTES` are parsed, decoded as UTF-8. Modified dates are
    preferred over published dates.

    Args:
        data: Raw HTML bytes.

    Returns:
        The title and date found, each empty if missing.
    """
    parser = _MetadataParser()
    parser.feed(data[:MAX_PARSE_BYTES].decode("utf-8", errors="replace"))
    title = " ".join("".join(parser.title_parts).split())[:200]
    date = ""
    for name in _DATE_META:
        match = _ISO_DATE.match(parser.meta.get(name, ""))
        if match:
            date = "-".join(match.groups())
            break
    return HtmlMetadata(title=title, date=date)
