"""Evidence and semantic checks that turn model output into a validated draft.

Schema-valid output is not necessarily supported by the sources, so every factual field
must cite known passages, and contact values must literally appear in the cited text.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .content import (
    OFFER_TEXT_FIELDS,
    CitedText,
    Evidence,
    FieldValue,
    GeneratedContent,
    Issue,
    Offer,
    Question,
    SubcategoryDraft,
)

MAX_OFFERS = 30
MAX_QUESTIONS = 40
MAX_QUESTION_LENGTH = 200
PASSAGE_EXCERPT_CHARS = 500
SINGLE_LINE_FIELDS = frozenset({"name", "chapter"})

_HTML_TAG = re.compile(r"<[^>]*>")
_TOP_HEADING = re.compile(r"^#{1,2}(?=\s)", re.MULTILINE)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_UNSAFE_LINK = re.compile(r"\[([^\]]*)\]\((?!https?://)[^)]*\)")
_CITATION = re.compile(r"[\[(]?\bS\d+(?:-[ps]\d+)?-c\d+\b[\])]?")
_BLANK_LINES = re.compile(r"\n{3,}")
_DIGIT_SEPARATORS = re.compile(r"(?<=\d)[\s().\-/]+(?=\d)")
_DIGIT_RUN = re.compile(r"\d+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ISSUE_LABELS = {
    "missing": "Missing information",
    "conflict": "Conflicting sources",
    "outdated": "Possibly outdated",
    "uncertain_translation": "Uncertain translation",
    "unsupported": "Not supported by sources",
    "unverified": "Value not found in sources",
    "structure": "Structure problem",
    "truncated": "Too many items",
}


@dataclass(frozen=True)
class ChunkRef:
    """A source passage the model may cite.

    Attributes:
        chunk_id: Citation ID shown to the model, e.g. "S1-p3-c2".
        source_id: ID of the source.
        source_ref: Short source reference, e.g. "S1".
        source_title: Title of the source.
        page: Page or sheet number, or None when the format has no pages.
        sheet: Sheet name for spreadsheets, otherwise "".
        text: Full passage text, used to verify contact values.
    """

    chunk_id: str
    source_id: str
    source_ref: str
    source_title: str
    page: int | None
    sheet: str
    text: str


class ChunkIndex:
    """Look up the passages that were given to the model by chunk ID.

    Args:
        refs: The passages included in the prompt.
    """

    def __init__(self, refs: list[ChunkRef]) -> None:
        self._by_id = {ref.chunk_id: ref for ref in refs}

    def __contains__(self, chunk_id: str) -> bool:
        return chunk_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def valid(self, chunk_ids: list[str]) -> list[str]:
        """Return the known chunk IDs, without brackets and duplicates, in cited order."""
        cleaned = (chunk_id.strip().strip("[]") for chunk_id in chunk_ids)
        return list(dict.fromkeys(c for c in cleaned if c in self._by_id))

    def evidence(self, chunk_ids: list[str]) -> list[Evidence]:
        """Return evidence with a passage excerpt for each known cited chunk ID."""
        return [
            Evidence(
                chunk_id=ref.chunk_id,
                source_id=ref.source_id,
                source_ref=ref.source_ref,
                source_title=ref.source_title,
                page=ref.page,
                sheet=ref.sheet,
                passage=ref.text[:PASSAGE_EXCERPT_CHARS],
            )
            for ref in (self._by_id[chunk_id] for chunk_id in self.valid(chunk_ids))
        ]

    def text_of(self, chunk_ids: list[str]) -> str:
        """Return the full text of the known cited passages, one per line."""
        return "\n".join(self._by_id[chunk_id].text for chunk_id in self.valid(chunk_ids))


def clean_markdown(text: str) -> str:
    """Reduce text to conservative Markdown.

    Removes HTML, images, citation labels and links that are not http(s), demotes top-level
    headings to level 3, and collapses extra blank lines.

    Args:
        text: Model or reviewer text.

    Returns:
        The cleaned, stripped text.
    """
    text = _HTML_TAG.sub("", text)
    text = _IMAGE.sub("", text)
    text = _UNSAFE_LINK.sub(r"\1", text)
    text = _TOP_HEADING.sub("###", text)
    text = _CITATION.sub("", text)
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def clean_line(text: str) -> str:
    """Clean text like `clean_markdown` and flatten it to one line without "#" signs."""
    return " ".join(clean_markdown(text).replace("#", " ").split())


def safe_ref(text: str) -> str:
    """Turn a model-supplied question ref into a short ID of letters, digits, "_" and "-"."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-")[:40]


def _digit_runs(text: str) -> list[str]:
    return _DIGIT_RUN.findall(_DIGIT_SEPARATORS.sub("", text))


def phone_supported(value: str, source_text: str) -> bool:
    """Check that a phone number's digits literally appear in the cited text.

    Separators are ignored. Numbers of up to 6 digits must match a whole digit run; longer
    numbers only need their last 8 digits to appear, since international prefixes vary.

    Args:
        value: Phone number from the model.
        source_text: Text of the cited passages.

    Returns:
        True if the number is supported; numbers with fewer than 3 digits never are.
    """
    digits = re.sub(r"\D", "", value)
    if len(digits) < 3:
        return False
    runs = _digit_runs(source_text)
    if len(digits) <= 6:
        return digits in runs
    # International prefixes are often written differently, so compare the local part.
    tail = digits[-8:]
    return any(tail in run for run in runs)


def normalize_email(value: str) -> str:
    """Return the email address without a "mailto:" prefix, or "" if it is not valid."""
    value = value.strip().removeprefix("mailto:").strip()
    return value if _EMAIL.match(value) else ""


def email_supported(value: str, source_text: str) -> bool:
    """Check, case-insensitively, that the email address appears in the cited text."""
    return value.casefold() in source_text.casefold()


def normalize_url(value: str) -> str:
    """Return an http(s) URL, adding "https://" to bare domains, or "" if it is not valid."""
    value = value.strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        if "://" in value or not re.match(r"^[\w.-]+\.[a-z]{2,}", value, re.IGNORECASE):
            return ""
        value = f"https://{value}"
    parts = urlsplit(value)
    return value if parts.scheme.lower() in ("http", "https") and parts.netloc else ""


def _url_core(value: str) -> str:
    parts = urlsplit(value)
    host = parts.netloc.casefold().removeprefix("www.")
    return f"{host}{parts.path}".rstrip("/")


def url_supported(value: str, source_text: str) -> bool:
    """Check that the URL's host and path appear in the cited text, ignoring scheme and www."""
    text = source_text.casefold().replace("www.", "")
    return _url_core(value).casefold() in text


@dataclass
class _Checker:
    index: ChunkIndex
    issues: list[Issue]

    def text(self, value: CitedText, field: str, item: str) -> FieldValue:
        cleaned = (
            clean_line(value.text) if field in SINGLE_LINE_FIELDS else clean_markdown(value.text)
        )
        if not cleaned:
            return FieldValue()
        evidence = self.index.evidence(value.chunk_ids)
        if not evidence:
            self.issues.append(
                Issue(
                    kind="unsupported",
                    item=item,
                    field=field,
                    description="Removed because it cited no valid source passage.",
                )
            )
            return FieldValue()
        return FieldValue(text=cleaned, evidence=evidence)

    def values(
        self,
        values: list[CitedText],
        field: str,
        item: str,
        normalize: Callable[[str], str],
        supported: Callable[[str, str], bool],
    ) -> list[FieldValue]:
        kept: list[FieldValue] = []
        seen: set[str] = set()
        for value in values:
            text = normalize(clean_line(value.text))
            if not text or text.casefold() in seen:
                continue
            evidence = self.index.evidence(value.chunk_ids)
            if not evidence:
                description = f"Removed “{text}” because it cited no valid source passage."
                self.issues.append(
                    Issue(kind="unsupported", item=item, field=field, description=description)
                )
                continue
            if not supported(text, self.index.text_of(value.chunk_ids)):
                description = f"Removed “{text}” because it does not appear in the cited passages."
                self.issues.append(
                    Issue(
                        kind="unverified",
                        item=item,
                        field=field,
                        description=description,
                        evidence=evidence,
                    )
                )
                continue
            seen.add(text.casefold())
            kept.append(FieldValue(text=text, evidence=evidence))
        return kept


def _offers(draft: SubcategoryDraft, checker: _Checker) -> list[Offer]:
    offers: list[Offer] = []
    for raw in draft.offers[:MAX_OFFERS]:
        label = clean_line(raw.name.text) or "Unnamed offer"
        offer = Offer()
        for field in OFFER_TEXT_FIELDS:
            setattr(offer, field, checker.text(getattr(raw, field), field, label))
        offer.phone_numbers = checker.values(
            raw.phone_numbers, "phone_numbers", label, lambda value: value, phone_supported
        )
        offer.emails = checker.values(raw.emails, "emails", label, normalize_email, email_supported)
        offer.web_urls = checker.values(
            raw.web_urls, "web_urls", label, normalize_url, url_supported
        )
        if not offer.name.text:
            checker.issues.append(
                Issue(
                    kind="unsupported",
                    item=label,
                    field="name",
                    description="Offer left out because its name is not supported by the sources.",
                )
            )
            continue
        offers.append(offer)
    if len(draft.offers) > MAX_OFFERS:
        checker.issues.append(
            Issue(kind="truncated", description=f"Only the first {MAX_OFFERS} offers were kept.")
        )
    return offers


def _questions(draft: SubcategoryDraft, checker: _Checker) -> list[Question]:
    questions: list[Question] = []
    refs: set[str] = set()
    for number, raw in enumerate(draft.questions[:MAX_QUESTIONS], start=1):
        text = clean_line(raw.question)[:MAX_QUESTION_LENGTH]
        if not text:
            continue
        answer = checker.text(raw.answer, "answer", text)
        if not answer.text:
            continue
        ref = safe_ref(raw.ref) or f"q{number}"
        if ref in refs:
            suffix = number
            while (candidate := f"{ref[:34]}-{suffix}") in refs:
                suffix += 1
            ref = candidate
        refs.add(ref)
        questions.append(
            Question(ref=ref, parent_ref=safe_ref(raw.parent_ref), question=text, answer=answer)
        )
    if len(draft.questions) > MAX_QUESTIONS:
        checker.issues.append(
            Issue(
                kind="truncated", description=f"Only the first {MAX_QUESTIONS} questions were kept."
            )
        )

    by_ref = {question.ref: question for question in questions}
    for question in questions:
        if question.parent_ref and (
            question.parent_ref not in by_ref or has_cycle(question, by_ref)
        ):
            question.parent_ref = ""
            checker.issues.append(
                Issue(
                    kind="structure",
                    item=question.question,
                    field="question",
                    description="Shown as a top-level question because its parent was invalid.",
                )
            )
    return questions


def has_cycle(question: Question, by_ref: dict[str, Question]) -> bool:
    """Check whether following the question's parents leads back to a visited question.

    Args:
        question: The question to start from.
        by_ref: All questions of the sub-category by ref.

    Returns:
        True if the parent chain contains a cycle.
    """
    seen = {question.ref}
    current = by_ref.get(question.parent_ref)
    while current is not None:
        if current.ref in seen:
            return True
        seen.add(current.ref)
        current = by_ref.get(current.parent_ref) if current.parent_ref else None
    return False


def check_draft(
    draft: SubcategoryDraft, index: ChunkIndex, *, generated_on: str, prompt_version: str
) -> GeneratedContent:
    """Turn a model draft into validated content that only keeps source-supported claims.

    Every text field must cite at least one known passage, or it is removed. Phone numbers,
    emails and web addresses must also literally appear in the cited passages. Offers
    without a supported name are left out, item counts are capped, and questions with an
    invalid or cyclic parent become top-level. Every removal is recorded as an issue.

    Args:
        draft: Structured model output for one sub-category.
        index: The passages that were given to the model.
        generated_on: Generation date (ISO format) to store with the content.
        prompt_version: Version of the prompt that produced the draft.

    Returns:
        The validated content with model and checker issues; issues are linked to the
        affected offer or question by matching name or question text.
    """
    checker = _Checker(index=index, issues=[])
    offers = _offers(draft, checker)
    questions = _questions(draft, checker)
    model_issues = [
        Issue(
            kind=raw.kind,
            item=clean_line(raw.item),
            field=clean_line(raw.field),
            description=clean_markdown(raw.description)[:1000],
            evidence=index.evidence(raw.chunk_ids),
        )
        for raw in draft.issues
        if clean_markdown(raw.description)
    ]
    issues = model_issues + checker.issues
    items = {offer.name.text.casefold(): offer.id for offer in offers}
    items.update({question.question.casefold(): question.ref for question in questions})
    for issue in issues:
        issue.item_ref = items.get(issue.item.casefold(), "")
    return GeneratedContent(
        offers=offers,
        questions=questions,
        issues=issues,
        generated_on=generated_on,
        prompt_version=prompt_version,
    )
