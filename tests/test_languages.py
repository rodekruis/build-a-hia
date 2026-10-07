import pytest

from build_a_hia.services.context import default_direction
from build_a_hia.services.languages import (
    LANGUAGES,
    find_language,
    language_label,
    normalize_code,
    resolve_language,
)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Tigrinya", "ti"),
        ("arabisch", "ar"),
        ("  Oekraiens ", "uk"),
        ("Kurdish (Sorani)", "ckb"),
        ("sorani", "ckb"),
        ("Farsi", "fa"),
        ("AM", "am"),
        ("pt_br", "pt-BR"),
        ("sr-latn-rs", "sr-Latn-RS"),
        ("tvl", "tvl"),
    ],
)
def test_names_and_codes_resolve(text, code):
    assert resolve_language(text) == code


@pytest.mark.parametrize("text", ["", "Klingonese", "en-", "english please", "x1"])
def test_unknown_text_is_rejected(text):
    assert resolve_language(text) is None


def test_table_has_unique_codes_names_and_valid_directions():
    codes = [language.code for language in LANGUAGES]
    assert len(codes) == len(set(codes))
    assert all(normalize_code(code) == code for code in codes)
    assert {language.direction for language in LANGUAGES} <= {"ltr", "rtl", "auto"}


def test_regional_codes_use_the_base_language():
    arabic = find_language("ar-SY")
    assert arabic is not None
    assert arabic.name == "Arabic"
    assert default_direction("ar-SY") == "rtl"
    assert default_direction("tvl") == "auto"
    assert language_label("ar") == "Arabic"
    assert language_label("ar-SY") == "ar-SY"
