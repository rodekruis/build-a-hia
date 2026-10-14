"""Independent programme-scope verdicts and deterministic withholding of rejected facts."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from .content import OFFER_LIST_FIELDS, OFFER_TEXT_FIELDS, CitedText, IssueDraft, SubcategoryDraft


class ScopeVerdict(BaseModel):
    """Required verdict for one draft value, identified by its position, not its wording."""

    value_id: str
    verdict: Literal["applies", "other_programme", "uncertain"]
    reason: str


class ContactConflict(BaseModel):
    """Alternative values for the same service/contact role, not additional contacts."""

    item: str
    field: Literal["phone_numbers", "emails", "web_urls"]
    candidates: list[CitedText]
    value_ids: list[str]


class ContactRewrite(BaseModel):
    """A narrowly corrected prose value containing only the preferred contact."""

    value_id: str
    replacement: CitedText


class ScopeReview(BaseModel):
    """Structured output of the independent scope and contact-conflict check."""

    verdicts: list[ScopeVerdict]
    contact_conflicts: list[ContactConflict]
    contact_rewrites: list[ContactRewrite]


class ScopeReviewError(Exception):
    """The scope check did not reliably assess every populated draft value."""


@dataclass(frozen=True)
class ScopeValue:
    """One value to assess, with the offer name or question wording as its context."""

    value_id: str
    item: str
    field: str
    value: CitedText


def scope_values(draft: SubcategoryDraft) -> list[ScopeValue]:
    """List every nonempty factual value, including individual contact entries."""
    values = []
    for number, offer in enumerate(draft.offers):
        for field in OFFER_TEXT_FIELDS:
            value = getattr(offer, field)
            if value.text.strip():
                values.append(ScopeValue(f"o{number}.{field}", offer.name.text, field, value))
        for field in OFFER_LIST_FIELDS:
            for entry, value in enumerate(getattr(offer, field)):
                if value.text.strip():
                    values.append(
                        ScopeValue(f"o{number}.{field}.{entry}", offer.name.text, field, value)
                    )
    for number, question in enumerate(draft.questions):
        if question.answer.text.strip():
            values.append(
                ScopeValue(f"q{number}.answer", question.question, "answer", question.answer)
            )
    return values


def apply_scope_review(draft: SubcategoryDraft, review: ScopeReview) -> SubcategoryDraft:
    """Withhold rejected values and retain their original citations as review gaps.

    Incomplete, duplicate or unknown verdicts fail the job rather than saving unaudited
    facts. The normal evidence checker subsequently drops nameless offers and unanswered
    questions and repairs follow-ups whose parent was removed.
    """
    result = draft.model_copy(deep=True)
    values = {value.value_id: value for value in scope_values(result)}
    ids = [verdict.value_id for verdict in review.verdicts]
    if len(ids) != len(set(ids)) or set(ids) != set(values):
        raise ScopeReviewError("The scope check did not assess every value exactly once.")
    for verdict in review.verdicts:
        if verdict.verdict == "applies":
            continue
        if not verdict.reason.strip():
            raise ScopeReviewError("The scope check did not explain a rejected value.")
        target = values[verdict.value_id]
        kind = "wrong_programme" if verdict.verdict == "other_programme" else "uncertain_scope"
        result.issues.append(
            IssueDraft(
                kind=kind,
                item=target.item,
                field=target.field,
                description=f"Withheld from public content. {verdict.reason.strip()}",
                chunk_ids=list(target.value.chunk_ids),
            )
        )
        target.value.text = ""
        target.value.chunk_ids = []
    return result
