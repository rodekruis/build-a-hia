"""Resolve semantically identified contact alternatives using source metadata, not model choice."""

import re
from dataclasses import dataclass
from datetime import date

from .checks import (
    ChunkIndex,
    clean_line,
    clean_markdown,
    normalize_email,
    normalize_url,
    phone_supported,
)
from .content import CitedText, IssueDraft, SubcategoryDraft
from .prompts import SourceMaterial
from .scope import ContactConflict, ScopeReview, ScopeReviewError, scope_values

_PHONE_IN_TEXT = re.compile(r"(?<!\w)\+?\d[\d\s()./\-]*\d(?!\w)")
_EMAIL_IN_TEXT = re.compile(r"[\w.+\-]+@[\w.\-]+\.\w+")
_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]]+", re.IGNORECASE)


def _key(field: str, text: str) -> str:
    if field == "phone_numbers":
        digits = re.sub(r"\D", "", text)
        return digits[-8:] if len(digits) > 6 else digits
    if field == "emails":
        return normalize_email(text).casefold()
    url = normalize_url(text).casefold()
    return url.removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")


def _contains(field: str, contact: str, text: str) -> bool:
    if field == "phone_numbers":
        return phone_supported(contact, text)
    if field == "emails":
        return bool(
            re.search(rf"(?<![\w.+\-]){re.escape(contact)}(?![\w\-]|\.\w)", text, re.IGNORECASE)
        )
    core = _key(field, contact)
    return bool(
        re.search(
            rf"(?<![\w.\-]){re.escape(core)}(?![\w/?#=&%\-]|\.\w)",
            text.casefold().replace("www.", ""),
        )
    )


def _new_contacts_supported(text: str, original: str, source_text: str) -> bool:
    for field, pattern in (
        ("phone_numbers", _PHONE_IN_TEXT),
        ("emails", _EMAIL_IN_TEXT),
        ("web_urls", _URL_IN_TEXT),
    ):
        for match in pattern.finditer(text):
            value = match.group().rstrip(".,;:")
            if field == "phone_numbers" and len(re.sub(r"\D", "", value)) < 3:
                continue
            if not _contains(field, value, original) and not _contains(field, value, source_text):
                return False
    return True


def _date(source: SourceMaterial) -> date | None:
    if not source.date:
        return None
    try:
        return date.fromisoformat(source.date.split()[0])
    except ValueError as error:
        raise ScopeReviewError("A contact source has an invalid document date.") from error


@dataclass(frozen=True)
class _Resolution:
    conflict: ContactConflict
    preferred: CitedText | None

    @property
    def rejected(self) -> list[CitedText]:
        selected = _key(self.conflict.field, self.preferred.text) if self.preferred else ""
        return [
            candidate
            for candidate in self.conflict.candidates
            if _key(self.conflict.field, candidate.text) != selected
        ]


def _resolve(
    conflict: ContactConflict, sources: list[SourceMaterial], index: ChunkIndex
) -> _Resolution:
    by_chunk = {chunk.chunk_id: source for source in sources for chunk in source.chunks}
    candidates = []
    for raw in conflict.candidates:
        text = clean_line(raw.text)
        if conflict.field == "emails":
            text = normalize_email(text)
        elif conflict.field == "web_urls":
            text = normalize_url(text)
        ids = index.valid(raw.chunk_ids)
        if (
            not text
            or not ids
            or len(ids) != len(set(raw.chunk_ids))
            or any(chunk_id not in by_chunk for chunk_id in ids)
            or not _contains(conflict.field, text, index.text_of(ids))
            or len({by_chunk[chunk_id].reference for chunk_id in ids}) != 1
        ):
            raise ScopeReviewError("A contact alternative has invalid source support.")
        candidates.append((CitedText(text=text, chunk_ids=ids), by_chunk[ids[0]]))
    if len({_key(conflict.field, value.text) for value, _source in candidates}) < 2:
        raise ScopeReviewError("A contact conflict needs at least two different supported values.")
    normalized = conflict.model_copy(update={"candidates": [value for value, _s in candidates]})
    dated = all(_date(source) is not None for _value, source in candidates)
    ranks = [
        ((_date(source) or date.min) if dated else date.min, source.format_label == "Web page")
        for _value, source in candidates
    ]
    best = max(ranks)
    winners = [
        value for (value, _source), rank in zip(candidates, ranks, strict=True) if rank == best
    ]
    preferred = winners[0] if len({_key(conflict.field, v.text) for v in winners}) == 1 else None
    return _Resolution(normalized, preferred)


def apply_contact_review(
    draft: SubcategoryDraft,
    review: ScopeReview,
    sources: list[SourceMaterial],
    index: ChunkIndex,
) -> SubcategoryDraft:
    """Use the newest dated contact, or a web page when dates are missing.

    Ties without a preferred value are withheld. Prose rewrites are accepted only when
    cited, contain every selected contact and contain none of the rejected alternatives.
    Missing/unsafe rewrites are withheld with a gap, never silently published.
    """
    result = draft.model_copy(deep=True)
    resolutions = [_resolve(conflict, sources, index) for conflict in review.contact_conflicts]
    values = scope_values(result)
    by_id = {value.value_id: value for value in values}
    rewrites = {rewrite.value_id: rewrite.replacement for rewrite in review.contact_rewrites}
    reviewed_ids = {verdict.value_id for verdict in review.verdicts}
    if len(rewrites) != len(review.contact_rewrites) or any(
        value_id not in by_id for value_id in rewrites
    ):
        raise ScopeReviewError("A contact rewrite has an unknown or duplicate value ID.")
    for resolution in resolutions:
        ids = resolution.conflict.value_ids
        if not ids or len(ids) != len(set(ids)) or not set(ids).issubset(reviewed_ids):
            raise ScopeReviewError("A contact conflict has invalid affected value IDs.")
    recorded: list[_Resolution] = []
    for target in values:
        related = [
            resolution
            for resolution in resolutions
            if target.value_id in resolution.conflict.value_ids
        ]
        if not related:
            continue
        needs_change = any(
            _contains(resolution.conflict.field, candidate.text, target.value.text)
            for resolution in related
            for candidate in resolution.rejected
        )
        is_contact = target.field in {"phone_numbers", "emails", "web_urls"}
        if is_contact and any(resolution.conflict.field != target.field for resolution in related):
            raise ScopeReviewError("A contact conflict targets the wrong contact field.")
        if is_contact and len(related) == 1 and related[0].conflict.field == target.field:
            replacement = related[0].preferred
        elif needs_change:
            replacement = rewrites.get(target.value_id)
        else:
            replacement = CitedText(
                text=target.value.text,
                chunk_ids=list(
                    dict.fromkeys(
                        [
                            *target.value.chunk_ids,
                            *(
                                chunk_id
                                for resolution in related
                                if resolution.preferred is not None
                                for chunk_id in resolution.preferred.chunk_ids
                            ),
                        ]
                    )
                ),
            )
        if replacement is not None:
            replacement = replacement.model_copy(update={"text": clean_markdown(replacement.text)})
        safe = replacement is not None and bool(clean_markdown(replacement.text))
        if target.field == "answer" and any(
            _contains(resolution.conflict.field, rejected.text, target.item)
            for resolution in related
            for rejected in resolution.rejected
        ):
            safe = False
        if replacement is not None:
            ids = index.valid(replacement.chunk_ids)
            safe = safe and bool(ids) and len(ids) == len(set(replacement.chunk_ids))
            safe = safe and _new_contacts_supported(
                replacement.text, target.value.text, index.text_of(ids)
            )
            for resolution in related:
                preferred = resolution.preferred
                safe = (
                    safe
                    and preferred is not None
                    and _contains(resolution.conflict.field, preferred.text, replacement.text)
                    and set(preferred.chunk_ids).issubset(ids)
                    and not any(
                        _contains(resolution.conflict.field, value.text, replacement.text)
                        for value in resolution.rejected
                    )
                )
        target.value.text = replacement.text if safe and replacement is not None else ""
        target.value.chunk_ids = (
            list(
                dict.fromkeys(
                    [
                        *(index.valid(target.value.chunk_ids) if not is_contact else []),
                        *replacement.chunk_ids,
                    ]
                )
            )
            if safe and replacement is not None
            else []
        )
        for resolution in related:
            recorded.append(resolution)
            alternatives = "; ".join(
                f"{value.text} ({', '.join(value.chunk_ids)})"
                for value in resolution.conflict.candidates
            )
            preferred = resolution.preferred
            choice = (
                f"Preferred contact: {preferred.text}."
                if preferred
                else "No source has clear priority; the contact was withheld."
            )
            result.issues.append(
                IssueDraft(
                    kind="conflict",
                    item=target.item,
                    field=target.field,
                    description=(
                        f"Conflicting contacts for {resolution.conflict.item}. {choice} "
                        f"Alternatives: {alternatives}. "
                        + ("" if safe else "This field was withheld pending review.")
                    ),
                    chunk_ids=list(
                        dict.fromkeys(
                            chunk_id
                            for value in resolution.conflict.candidates
                            for chunk_id in value.chunk_ids
                        )
                    ),
                )
            )
    for resolution in resolutions:
        if resolution not in recorded:
            result.issues.append(
                IssueDraft(
                    kind="conflict",
                    item=resolution.conflict.item,
                    field=resolution.conflict.field,
                    description=(
                        "Conflicting contact values require review: "
                        + "; ".join(value.text for value in resolution.conflict.candidates)
                    ),
                    chunk_ids=list(
                        dict.fromkeys(
                            chunk_id
                            for value in resolution.conflict.candidates
                            for chunk_id in value.chunk_ids
                        )
                    ),
                )
            )
    return result
