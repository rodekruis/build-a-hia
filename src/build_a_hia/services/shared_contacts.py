"""Apply independently assessed programme contacts without overriding specific fields."""

from .checks import (
    ChunkIndex,
    clean_line,
    clean_markdown,
    email_supported,
    normalize_email,
    normalize_url,
    phone_supported,
    url_supported,
)
from .content import OFFER_LIST_FIELDS, CitedText, SubcategoryDraft
from .scope import ScopeReview, ScopeReviewError


def apply_shared_contacts(
    draft: SubcategoryDraft, review: ScopeReview, index: ChunkIndex
) -> tuple[SubcategoryDraft, dict[tuple[int, str], str]]:
    """Fill empty contact fields with cited, explicitly applicable shared defaults.

    The independent review assesses programme membership and shared applicability.
    Code validates target identity, evidence and literal contact values, and never
    rescues a field rejected by the scope check.
    """
    result = draft.model_copy(deep=True)
    inherited: dict[tuple[int, str], str] = {}
    for shared in review.shared_contacts:
        number, field = shared.offer_index, shared.field
        if not 0 <= number < len(result.offers) or (number, field) in inherited:
            raise ScopeReviewError("A shared contact has an invalid or duplicate offer field.")
        offer = result.offers[number]
        existing = getattr(offer, field)
        populated = (
            any(v.text.strip() for v in existing)
            if isinstance(existing, list)
            else existing.text.strip()
        )
        rejected = any(
            verdict.verdict != "applies"
            and (
                verdict.value_id == f"o{number}.name"
                or verdict.value_id == f"o{number}.{field}"
                or verdict.value_id.startswith(f"o{number}.{field}.")
            )
            for verdict in review.verdicts
        )
        if populated or rejected or not offer.name.text.strip():
            raise ScopeReviewError(
                "A shared contact cannot override an existing or rejected field."
            )
        applicability = index.valid(shared.applicability_chunk_ids)
        if (
            not clean_line(shared.programme)
            or not clean_markdown(shared.reason)
            or not applicability
            or len(applicability) != len(set(shared.applicability_chunk_ids))
            or not shared.values
            or (field not in OFFER_LIST_FIELDS and len(shared.values) != 1)
        ):
            raise ScopeReviewError("A shared contact lacks programme applicability evidence.")
        values = []
        for raw in shared.values:
            text = clean_line(raw.text) if field in OFFER_LIST_FIELDS else clean_markdown(raw.text)
            ids = index.valid(raw.chunk_ids)
            source_text = index.text_of(ids)
            if field == "emails":
                text = normalize_email(text)
                supported = bool(text) and email_supported(text, source_text)
            elif field == "phone_numbers":
                supported = phone_supported(text, source_text)
            elif field == "web_urls":
                text = normalize_url(text)
                supported = bool(text) and url_supported(text, source_text)
            else:
                supported = bool(text)
            if not supported or not ids or len(ids) != len(set(raw.chunk_ids)):
                raise ScopeReviewError("A shared contact lacks valid source support.")
            values.append(
                CitedText(text=text, chunk_ids=list(dict.fromkeys([*ids, *applicability])))
            )
        setattr(offer, field, values if field in OFFER_LIST_FIELDS else values[0])
        inherited[number, field] = clean_line(shared.programme)
    return result, inherited
