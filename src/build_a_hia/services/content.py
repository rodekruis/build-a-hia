"""Pydantic schemas for model output and for validated, stored drafts.

Model output schemas use only required fields so they work with strict structured output.
"""

import secrets
from typing import Literal

from pydantic import BaseModel, Field

IssueKind = Literal["missing", "conflict", "outdated", "uncertain_translation"]


def new_item_id() -> str:
    """Return a random 8-character hex ID for an offer or issue."""
    return secrets.token_hex(4)


class ProposedSubcategory(BaseModel):
    """Model output: a proposed sub-category.

    Attributes:
        name: Sub-category name.
        description: Short description.
        chunk_ids: IDs of the passages that support it.
    """

    name: str
    description: str
    chunk_ids: list[str]


class ProposedCategory(BaseModel):
    """Model output: a proposed category with its sub-categories."""

    name: str
    description: str
    subcategories: list[ProposedSubcategory]


class StructureProposal(BaseModel):
    """Model output: a proposed category structure.

    Attributes:
        categories: Proposed categories.
        notes: The model's notes for the reviewer about the proposal.
    """

    categories: list[ProposedCategory]
    notes: str


class CitedText(BaseModel):
    """Model output: a text value with its citations.

    Attributes:
        text: The value; "" when the sources do not provide it.
        chunk_ids: IDs of the passages that support the text. Values without a valid
            citation are removed by the evidence checks.
    """

    text: str
    chunk_ids: list[str]


class OfferDraft(BaseModel):
    """Model output: one offer, with every field cited separately."""

    name: CitedText
    description: CitedText
    phone_numbers: list[CitedText]
    emails: list[CitedText]
    web_urls: list[CitedText]
    address: CitedText
    open_weekdays: CitedText
    open_weekend: CitedText
    need_to_know: CitedText
    more_info: CitedText
    chapter: CitedText


class QuestionDraft(BaseModel):
    """Model output: one Q&A.

    Attributes:
        ref: The model's own short ID for the question, used by follow-ups.
        parent_ref: `ref` of the parent question for a follow-up, or "" for a top-level
            question.
        question: Question text.
        answer: Answer text (Markdown) with its citations.
    """

    ref: str
    parent_ref: str
    question: str
    answer: CitedText


class IssueDraft(BaseModel):
    """Model output: a problem the model noticed in the sources.

    Attributes:
        kind: Type of problem.
        item: Name of the affected offer or question text, or "".
        field: Affected field, or "".
        description: Explanation for the reviewer.
        chunk_ids: IDs of the related passages.
    """

    kind: IssueKind
    item: str
    field: str
    description: str
    chunk_ids: list[str]


class SubcategoryDraft(BaseModel):
    """Model output: offers, Q&As and issues for one sub-category."""

    offers: list[OfferDraft]
    questions: list[QuestionDraft]
    issues: list[IssueDraft]


class Evidence(BaseModel):
    """A source passage that supports a stored value.

    Attributes:
        chunk_id: Citation ID of the passage.
        source_id: ID of the source.
        source_ref: Short source reference, e.g. "S1".
        source_title: Title of the source.
        page: Page or sheet number, or None when the format has no pages.
        sheet: Sheet name for spreadsheets, otherwise "".
        passage: Excerpt of the passage text.
    """

    chunk_id: str
    source_id: str
    source_ref: str
    source_title: str
    page: int | None = None
    sheet: str = ""
    passage: str = ""


class FieldValue(BaseModel):
    """A validated value with the evidence that supports it.

    Attributes:
        text: The value; "" when empty.
        evidence: Supporting passages; empty for reviewer-added values.
        edited: Whether a reviewer changed or added the text; evidence then refers to the
            original text only.
        original: Text before the first reviewer edit; "" for reviewer-added values.
    """

    text: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    edited: bool = False
    original: str = ""


OFFER_TEXT_FIELDS = (
    "name",
    "description",
    "address",
    "open_weekdays",
    "open_weekend",
    "need_to_know",
    "more_info",
    "chapter",
)
OFFER_LIST_FIELDS = ("phone_numbers", "emails", "web_urls")
FIELD_LABELS = {
    "name": "Name",
    "description": "Description",
    "phone_numbers": "Phone numbers",
    "emails": "Emails",
    "web_urls": "Websites",
    "address": "Address",
    "open_weekdays": "Opening hours (weekdays)",
    "open_weekend": "Opening hours (weekend)",
    "need_to_know": "What you need to know",
    "more_info": "More information",
    "chapter": "Chapter",
    "question": "Question",
    "answer": "Answer",
}


class Offer(BaseModel):
    """A validated offer; fields map to the columns of the HIA Offers tab.

    Attributes:
        id: Internal item ID; the HIA #ID is assigned at assembly.
    """

    id: str = Field(default_factory=new_item_id)
    name: FieldValue = Field(default_factory=FieldValue)
    description: FieldValue = Field(default_factory=FieldValue)
    phone_numbers: list[FieldValue] = Field(default_factory=list)
    emails: list[FieldValue] = Field(default_factory=list)
    web_urls: list[FieldValue] = Field(default_factory=list)
    address: FieldValue = Field(default_factory=FieldValue)
    open_weekdays: FieldValue = Field(default_factory=FieldValue)
    open_weekend: FieldValue = Field(default_factory=FieldValue)
    need_to_know: FieldValue = Field(default_factory=FieldValue)
    more_info: FieldValue = Field(default_factory=FieldValue)
    chapter: FieldValue = Field(default_factory=FieldValue)


class Question(BaseModel):
    """A validated Q&A.

    Attributes:
        ref: Internal ID, unique within the sub-category; the slug is assigned at assembly.
        parent_ref: `ref` of the parent question, or "" for a top-level question.
        question: Question text.
        answer: Answer (Markdown) with its evidence.
        question_edited: Whether a reviewer changed or added the question text.
    """

    ref: str
    parent_ref: str = ""
    question: str
    answer: FieldValue
    question_edited: bool = False


class Issue(BaseModel):
    """A problem found by the model or the evidence checks, shown to reviewers as a gap.

    Attributes:
        id: Stable issue ID, also used as the gap ID.
        kind: Issue kind, e.g. "missing", "unsupported", "unverified" or "truncated".
        item: Name of the affected offer or question text, or "".
        item_ref: Offer id or question ref of the affected item, when known.
        field: Affected field, or "".
        description: Explanation for the reviewer.
        evidence: Related passages.
    """

    id: str = Field(default_factory=new_item_id)
    kind: str
    item: str = ""
    item_ref: str = ""
    field: str = ""
    description: str
    evidence: list[Evidence] = Field(default_factory=list)


class GeneratedContent(BaseModel):
    """Validated content of one sub-category, as stored and reviewed.

    Attributes:
        offers: Offers in display order.
        questions: Q&As; follow-ups refer to their parent by `parent_ref`.
        issues: Problems for the reviewer.
        generated_on: Generation date (ISO format).
        prompt_version: Version of the prompt that produced the content.
        updated_on: Date of the last reviewer edit (ISO format), or ""; used instead of
            `generated_on` for the Q&A #UPDATED column.
    """

    offers: list[Offer] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    generated_on: str
    prompt_version: str
    updated_on: str = ""

    @property
    def is_empty(self) -> bool:
        """Whether there are no offers and no questions."""
        return not self.offers and not self.questions
