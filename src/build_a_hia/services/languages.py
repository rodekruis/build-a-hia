"""Output languages: names in English and Dutch, codes for #locale.language and text direction.

Users type a language name or code; `resolve_language` turns it into a BCP 47 code. Codes
not in the table are accepted when they are well formed, such as regional variants.
"""

import re
import unicodedata
from dataclasses import dataclass

MAX_LANGUAGE_INPUT = 60
_CODE = re.compile(r"^([a-z]{2,3})(?:-([a-z]{4}))?(?:-([a-z]{2}|\d{3}))?$")


@dataclass(frozen=True)
class Language:
    """An output language.

    Attributes:
        code: BCP 47 code used for #locale.language.
        name: English name.
        dutch: Dutch name.
        direction: Default text direction: "ltr", "rtl", or "auto" when scripts differ.
        aliases: Other names that should resolve to this language.
    """

    code: str
    name: str
    dutch: str
    direction: str = "ltr"
    aliases: tuple[str, ...] = ()


LANGUAGES: tuple[Language, ...] = (
    Language("af", "Afrikaans", "Afrikaans"),
    Language("ak", "Akan", "Akan", aliases=("Twi",)),
    Language("sq", "Albanian", "Albanees", aliases=("Shqip",)),
    Language("am", "Amharic", "Amhaars"),
    Language("ar", "Arabic", "Arabisch", "rtl"),
    Language("hy", "Armenian", "Armeens"),
    Language("as", "Assamese", "Assamees"),
    Language("ay", "Aymara", "Aymara"),
    Language("az", "Azerbaijani", "Azerbeidzjaans", aliases=("Azeri",)),
    Language("bal", "Balochi", "Beloetsji", "rtl", aliases=("Baluchi",)),
    Language("bm", "Bambara", "Bambara"),
    Language("eu", "Basque", "Baskisch"),
    Language("be", "Belarusian", "Wit-Russisch", aliases=("Belarussisch",)),
    Language("bn", "Bengali", "Bengaals", aliases=("Bangla",)),
    Language("bho", "Bhojpuri", "Bhojpuri"),
    Language("bs", "Bosnian", "Bosnisch"),
    Language("bg", "Bulgarian", "Bulgaars"),
    Language("my", "Burmese", "Birmaans", aliases=("Myanmar",)),
    Language("yue", "Cantonese", "Kantonees"),
    Language("ca", "Catalan", "Catalaans"),
    Language("ceb", "Cebuano", "Cebuano"),
    Language("ny", "Chichewa", "Chichewa", aliases=("Nyanja", "Chewa")),
    Language("zh", "Chinese", "Chinees", aliases=("Mandarin",)),
    Language("hr", "Croatian", "Kroatisch"),
    Language("cs", "Czech", "Tsjechisch"),
    Language("da", "Danish", "Deens"),
    Language("prs", "Dari", "Dari", "rtl"),
    Language("dv", "Dhivehi", "Divehi", "rtl", aliases=("Maldivian",)),
    Language("din", "Dinka", "Dinka"),
    Language("nl", "Dutch", "Nederlands", aliases=("Flemish", "Vlaams")),
    Language("dz", "Dzongkha", "Dzongkha"),
    Language("en", "English", "Engels"),
    Language("et", "Estonian", "Estisch"),
    Language("ee", "Ewe", "Ewe"),
    Language("fil", "Filipino", "Filipijns"),
    Language("fi", "Finnish", "Fins"),
    Language("fr", "French", "Frans"),
    Language("fy", "Frisian", "Fries", aliases=("West Frisian", "Frysk")),
    Language("ff", "Fula", "Fula", aliases=("Fulani", "Fulfulde", "Pulaar")),
    Language("gl", "Galician", "Galicisch"),
    Language("lg", "Ganda", "Luganda", aliases=("Luganda",)),
    Language("ka", "Georgian", "Georgisch"),
    Language("de", "German", "Duits"),
    Language("el", "Greek", "Grieks"),
    Language("gn", "Guarani", "Guaraní"),
    Language("gu", "Gujarati", "Gujarati"),
    Language("ht", "Haitian Creole", "Haïtiaans Creools", aliases=("Kreyòl",)),
    Language("ha", "Hausa", "Hausa"),
    Language("he", "Hebrew", "Hebreeuws", "rtl"),
    Language("hi", "Hindi", "Hindi"),
    Language("hmn", "Hmong", "Hmong"),
    Language("hu", "Hungarian", "Hongaars"),
    Language("is", "Icelandic", "IJslands"),
    Language("ig", "Igbo", "Igbo"),
    Language("ilo", "Ilocano", "Ilocano"),
    Language("id", "Indonesian", "Indonesisch", aliases=("Bahasa Indonesia",)),
    Language("ga", "Irish", "Iers"),
    Language("it", "Italian", "Italiaans"),
    Language("ja", "Japanese", "Japans"),
    Language("jv", "Javanese", "Javaans"),
    Language("kab", "Kabyle", "Kabylisch"),
    Language("kn", "Kannada", "Kannada"),
    Language("kr", "Kanuri", "Kanuri"),
    Language("kk", "Kazakh", "Kazachs"),
    Language("km", "Khmer", "Khmer", aliases=("Cambodian",)),
    Language("ki", "Kikuyu", "Kikuyu", aliases=("Gikuyu",)),
    Language("rw", "Kinyarwanda", "Kinyarwanda"),
    Language("rn", "Kirundi", "Kirundi", aliases=("Rundi",)),
    Language("kg", "Kongo", "Kongo", aliases=("Kikongo",)),
    Language("ko", "Korean", "Koreaans"),
    Language("kri", "Krio", "Krio"),
    Language(
        "kmr",
        "Kurdish (Kurmanji)",
        "Koerdisch (Kurmanji)",
        aliases=("Kurmanji", "Northern Kurdish"),
    ),
    Language(
        "ckb",
        "Kurdish (Sorani)",
        "Koerdisch (Sorani)",
        "rtl",
        aliases=("Sorani", "Central Kurdish"),
    ),
    Language("ky", "Kyrgyz", "Kirgizisch"),
    Language("lo", "Lao", "Laotiaans"),
    Language("lv", "Latvian", "Lets"),
    Language("ln", "Lingala", "Lingala"),
    Language("lt", "Lithuanian", "Litouws"),
    Language("luo", "Luo", "Luo", aliases=("Dholuo",)),
    Language("lb", "Luxembourgish", "Luxemburgs"),
    Language("mk", "Macedonian", "Macedonisch"),
    Language("mg", "Malagasy", "Malagassisch"),
    Language("ms", "Malay", "Maleis"),
    Language("ml", "Malayalam", "Malayalam"),
    Language("mt", "Maltese", "Maltees"),
    Language("mi", "Maori", "Maori"),
    Language("mr", "Marathi", "Marathi"),
    Language("mn", "Mongolian", "Mongools"),
    Language("mos", "Mooré", "Mooré", aliases=("Mossi",)),
    Language("nd", "Ndebele (North)", "Noord-Ndebele", aliases=("North Ndebele",)),
    Language("ne", "Nepali", "Nepalees"),
    Language("pcm", "Nigerian Pidgin", "Nigeriaans Pidgin"),
    Language("nb", "Norwegian (Bokmål)", "Noors (Bokmål)", aliases=("Norwegian", "Noors")),
    Language("nus", "Nuer", "Nuer"),
    Language("or", "Odia", "Odia", aliases=("Oriya",)),
    Language("om", "Oromo", "Oromo", aliases=("Afaan Oromoo",)),
    Language("pap", "Papiamento", "Papiaments"),
    Language("ps", "Pashto", "Pasjtoe", "rtl", aliases=("Pushto",)),
    Language("fa", "Persian", "Perzisch", "rtl", aliases=("Farsi",)),
    Language("pl", "Polish", "Pools"),
    Language("pt", "Portuguese", "Portugees"),
    Language("pa", "Punjabi", "Punjabi"),
    Language("qu", "Quechua", "Quechua"),
    Language("rhg", "Rohingya", "Rohingya", "auto"),
    Language("ro", "Romanian", "Roemeens", aliases=("Moldovan",)),
    Language("rom", "Romani", "Romani"),
    Language("ru", "Russian", "Russisch"),
    Language("sm", "Samoan", "Samoaans"),
    Language("sg", "Sango", "Sango"),
    Language("sr", "Serbian", "Servisch"),
    Language("st", "Sesotho", "Sesotho", aliases=("Southern Sotho",)),
    Language("tn", "Setswana", "Tswana", aliases=("Tswana",)),
    Language("sn", "Shona", "Shona"),
    Language("sd", "Sindhi", "Sindhi", "rtl"),
    Language("si", "Sinhala", "Singalees", aliases=("Sinhalese",)),
    Language("sk", "Slovak", "Slowaaks"),
    Language("sl", "Slovenian", "Sloveens", aliases=("Slovene",)),
    Language("so", "Somali", "Somalisch"),
    Language("es", "Spanish", "Spaans", aliases=("Castilian",)),
    Language("srn", "Sranan Tongo", "Sranantongo", aliases=("Sranan",)),
    Language("su", "Sundanese", "Soendanees"),
    Language("sw", "Swahili", "Swahili", aliases=("Kiswahili",)),
    Language("sv", "Swedish", "Zweeds"),
    Language("tl", "Tagalog", "Tagalog"),
    Language("tg", "Tajik", "Tadzjieks"),
    Language("zgh", "Tamazight", "Tamazight", aliases=("Amazigh", "Berber")),
    Language("ta", "Tamil", "Tamil"),
    Language("tt", "Tatar", "Tataars"),
    Language("te", "Telugu", "Telugu"),
    Language("th", "Thai", "Thai"),
    Language("bo", "Tibetan", "Tibetaans"),
    Language("tig", "Tigre", "Tigre"),
    Language("ti", "Tigrinya", "Tigrinya"),
    Language("to", "Tongan", "Tongaans"),
    Language("ts", "Tsonga", "Tsonga"),
    Language("tr", "Turkish", "Turks"),
    Language("tk", "Turkmen", "Turkmeens"),
    Language("uk", "Ukrainian", "Oekraïens"),
    Language("ur", "Urdu", "Urdu", "rtl"),
    Language("ug", "Uyghur", "Oeigoers", "rtl", aliases=("Uighur",)),
    Language("uz", "Uzbek", "Oezbeeks"),
    Language("vi", "Vietnamese", "Vietnamees"),
    Language("cy", "Welsh", "Welsh"),
    Language("wo", "Wolof", "Wolof"),
    Language("xh", "Xhosa", "Xhosa"),
    Language("yi", "Yiddish", "Jiddisch", "rtl"),
    Language("yo", "Yoruba", "Yoruba"),
    Language("zu", "Zulu", "Zoeloe"),
)

_BY_CODE = {language.code: language for language in LANGUAGES}


def _key(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


_BY_NAME = {
    _key(name): language.code
    for language in LANGUAGES
    for name in (language.name, language.dutch, *language.aliases)
}


def normalize_code(text: str) -> str | None:
    """Return `text` as a well-formed BCP 47 code (such as "pt-BR"), or None.

    Only language, optional script and optional region subtags are accepted.
    """
    match = _CODE.match(text.strip().replace("_", "-").lower())
    if not match:
        return None
    language, script, region = match.groups()
    parts = [language]
    if script:
        parts.append(script.title())
    if region:
        parts.append(region.upper())
    return "-".join(parts)


def find_language(code: str) -> Language | None:
    """Return the language for a code, falling back to its base language (for "ar-SY")."""
    return _BY_CODE.get(code) or _BY_CODE.get(code.split("-", 1)[0])


def resolve_language(text: str) -> str | None:
    """Turn a language name (English or Dutch) or code into a BCP 47 code.

    Args:
        text: What the user typed, such as "Tigrinya", "Arabisch", "ckb" or "pt-BR".

    Returns:
        The code, or None when the text is neither a known name nor a well-formed code.
    """
    text = text.strip()[:MAX_LANGUAGE_INPUT]
    if not text:
        return None
    if text.lower() in _BY_CODE:
        return text.lower()
    if code := _BY_NAME.get(_key(text)):
        return code
    return normalize_code(text)


def language_label(code: str) -> str:
    """Return how to show a stored code in the form: the English name, or the code itself."""
    language = _BY_CODE.get(code)
    return language.name if language else code
