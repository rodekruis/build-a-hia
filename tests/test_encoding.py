from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "build_a_hia"
# UTF-8 text that was decoded as Windows-1252 and saved again, e.g. "Â·" for "·".
MOJIBAKE = ("Â", "â€", "Ã©", "Ã«", "\ufeff")


@pytest.mark.parametrize(
    "path",
    sorted([*PACKAGE.rglob("*.py"), *PACKAGE.rglob("*.html"), *PACKAGE.rglob("*.css")]),
    ids=lambda path: str(path.relative_to(PACKAGE)),
)
def test_source_files_are_clean_utf8(path):
    text = path.read_bytes().decode("utf-8")

    assert not [marker for marker in MOJIBAKE if marker in text]
