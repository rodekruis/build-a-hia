import io
import json
import secrets
import zipfile

from PIL import Image

from build_a_hia.services.model import ModelResult
from build_a_hia.services.sessions import session_key
from build_a_hia.services.sources import Source, SourceKind, SourceStatus, chunks_blob

SOURCE_TEXTS = [
    "The Red Cross night shelter at Central Station 5, Kyiv offers free beds. "
    "Call +380 44 123 4567 or email shelter@example.org. See https://example.org/shelter.",
    "Cash assistance: families registered after 1 March can apply at the town hall.",
]


def add_converted_source(services, key: str, texts: list[str], title: str = "Guide") -> Source:
    """Store a converted source whose chunks are S<n>-p<page>-c1, one per text."""
    number = services.sessions.allocate_source_number(key)
    source = Source(
        id=secrets.token_hex(8),
        number=number,
        kind=SourceKind.FILE,
        title=title,
        status=SourceStatus.CONVERTED,
        created_at="2026-10-07T00:00:00+00:00",
        format="pdf",
        page_count=len(texts),
        chunk_count=len(texts),
        token_count=50,
    )
    services.sources.repository.insert(key, source)
    chunks = [
        {"id": f"S{number}-p{page}-c1", "page": page, "sheet": "", "text": text, "tokens": 5}
        for page, text in enumerate(texts, start=1)
    ]
    services.blobs.put(chunks_blob(key, source.id), json.dumps(chunks).encode())
    return source


class FakeModel:
    """Returns scripted responses (dicts are validated against the requested schema)."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], type]] = []

    def parse(self, messages, schema):
        self.calls.append((messages, schema))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ModelResult(parsed=schema.model_validate(response), tokens=1000)


def cited(text: str, *chunk_ids: str) -> dict:
    return {"text": text, "chunk_ids": list(chunk_ids)}


def offer(name: dict, **fields) -> dict:
    empty = cited("")
    data = {
        "name": name,
        "description": empty,
        "phone_numbers": [],
        "emails": [],
        "web_urls": [],
        "address": empty,
        "open_weekdays": empty,
        "open_weekend": empty,
        "need_to_know": empty,
        "more_info": empty,
        "chapter": empty,
    }
    data.update(fields)
    return data


def question(ref: str, text: str, answer: dict, parent_ref: str = "") -> dict:
    return {"ref": ref, "parent_ref": parent_ref, "question": text, "answer": answer}


PROPOSAL = {
    "categories": [
        {
            "name": "Shelter",
            "description": "Places to sleep.",
            "subcategories": [
                {"name": "Night shelter", "description": "", "chunk_ids": ["S1-p1-c1", "S9-p9-c9"]}
            ],
        },
        {
            "name": "Money",
            "description": "",
            "subcategories": [
                {"name": "Cash assistance", "description": "", "chunk_ids": ["[S1-p2-c1]"]}
            ],
        },
    ],
    "notes": "No information about health.",
}

DRAFT_FOR_ROUTES = {
    "offers": [
        offer(
            cited("Night shelter", "S1-p1-c1"),
            description=cited("**Free** & open<script>x</script>", "S1-p1-c1"),
            need_to_know=cited("Bring a passport."),
        )
    ],
    "questions": [question("q1", "Who can stay?", cited("Anyone.", "S1-p1-c1"))],
    "issues": [],
}

CONTEXT_FORM = {
    "country": "Ukraine",
    "situation": "Displacement after shelling in the east.",
    "target_group": "Internally displaced people",
    "locations": "Dnipro, Kharkiv",
    "output_language": "uk",
    "locale_dir": "",
    "source_languages": "Ukrainian, English",
    "reference_date": "2026-10-01",
}


def session_key_of(client) -> str:
    cookie = client.get_cookie("hia_session")
    assert cookie is not None
    return session_key(cookie.value)


def upload(client, filename: str, data: bytes):
    return client.post(
        "/sources/files",
        data={"files": [(io.BytesIO(data), filename)]},
        content_type="multipart/form-data",
    )


def make_text_pdf(page_texts: list[str]) -> bytes:
    """Build a small valid PDF with one line of Helvetica text per page."""
    count = len(page_texts)
    page_ids = [4 + 2 * index for index in range(count)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(page_texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 14 Tf 72 760 Td ({escaped}) Tj ET".encode() if text else b""
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[index] + 1} 0 R >>"
            ).encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")

    output = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return output


def make_docx(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


def make_xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    first = True
    for name, rows in sheets.items():
        sheet = workbook.active if first else workbook.create_sheet()
        first = False
        sheet.title = name
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_png(size: tuple[int, int] = (20, 10)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()
