"""Project context that steers generation and fills the HIA locale settings."""

import json
from dataclasses import asdict, dataclass, fields

from .languages import Language, find_language

TEXT_DIRECTIONS = ("ltr", "rtl", "auto")


@dataclass(frozen=True)
class ProjectContext:
    """What the HIA is for, as described by the user.

    Attributes:
        country: Country the HIA covers.
        situation: Situation or crisis the HIA responds to.
        target_group: People the HIA is for.
        locations: Regions or locations the information should cover.
        output_language: BCP 47 code of the HIA content, such as "ar" or "pt-BR".
        locale_dir: Text direction for #locale.dir: "ltr", "rtl" or "auto".
        source_languages: Languages of the sources, if given.
        reference_date: ISO date on which the information should be valid, or "".
    """

    country: str
    situation: str
    target_group: str
    locations: str
    output_language: str
    locale_dir: str
    source_languages: str = ""
    reference_date: str = ""

    @property
    def language(self) -> Language | None:
        """The output language (or its base language for regional codes), if known."""
        return find_language(self.output_language)

    @property
    def locale_language(self) -> str:
        """Value for the HIA `#locale.language` setting."""
        return self.output_language

    def to_json(self) -> str:
        """Serialize the context to JSON."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "ProjectContext":
        """Load a context from JSON, ignoring unknown keys from other versions."""
        data = json.loads(raw)
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def default_direction(language_code: str) -> str:
    """Return the language's default text direction, or "auto" for unknown languages."""
    language = find_language(language_code)
    return language.direction if language else "auto"
