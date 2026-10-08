"""Step 7: reviewer edits, gaps and content approval.

Reviewer edits keep the original evidence but are flagged, so human changes stay separate
from source-supported claims. Gaps are collected from generation issues, empty
sub-categories and conversion problems; their status lives in per-gap table rows.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .assembly import AssembledCategory, AssembledSubcategory
from .checks import (
    ISSUE_LABELS,
    SINGLE_LINE_FIELDS,
    clean_line,
    clean_markdown,
    has_cycle,
    normalize_email,
    normalize_url,
)
from .content import (
    OFFER_LIST_FIELDS,
    OFFER_TEXT_FIELDS,
    Evidence,
    FieldValue,
    GeneratedContent,
    Offer,
    Question,
)
from .sources import Source, SourceStatus
from .storage import TableStore, mutate_entity

MAX_QUESTION_LENGTH = 200
MAX_FIELD_LENGTH = 5000
GAP_ROW_PREFIX = "gap:"
GAP_STATUSES = {"open": "Open", "resolved": "Resolved", "wont_fix": "Won't fix"}
GAP_KIND_LABELS = {
    **ISSUE_LABELS,
    "conversion": "Conversion problem",
    "empty": "No supporting content",
}
SUGGESTED_ACTIONS = {
    "missing": "Ask the program team for this information, or leave the field blank.",
    "conflict": "Check with program team which information is current.",
    "outdated": "Confirm that the information is still valid.",
    "uncertain_translation": "Ask a native speaker to check the translation.",
    "unsupported": "Find a source for this information, or leave it out.",
    "unverified": "Check the value against the source and add it manually if correct.",
    "structure": "Check the order and nesting of the questions.",
    "truncated": "Add the remaining items manually or split the sub-category.",
    "conversion": "Retry the conversion or add a better copy of the document.",
    "empty": "Decide whether to keep this sub-category without content.",
}


class ReviewError(Exception):
    """Raised when a reviewer edit is invalid or refers to an item that no longer exists.

    Args:
        message: User-facing explanation, also kept in the `message` attribute.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _edited(old: FieldValue, text: str) -> FieldValue:
    if text == old.text:
        return old
    original = old.original if old.edited else old.text
    if old.edited and text == original:
        return FieldValue(text=text, evidence=old.evidence)
    return FieldValue(text=text, evidence=old.evidence, edited=True, original=original)


def _clean_text(field: str, value: str) -> str:
    cleaned = clean_line(value) if field in SINGLE_LINE_FIELDS else clean_markdown(value)
    return cleaned[:MAX_FIELD_LENGTH]


def _clean_values(field: str, raw: str) -> list[str]:
    values: list[str] = []
    for line in raw.splitlines():
        text = clean_line(line)
        if not text:
            continue
        if field == "emails":
            normalized = normalize_email(text)
            if not normalized:
                raise ReviewError(f"“{text}” is not a valid email address.")
            text = normalized
        elif field == "web_urls":
            normalized = normalize_url(text)
            if not normalized:
                raise ReviewError(f"“{text}” is not a valid web address (use http or https).")
            text = normalized
        if text.casefold() not in (value.casefold() for value in values):
            values.append(text)
    return values


def _apply_offer(offer: Offer, values: dict[str, str]) -> None:
    for field in OFFER_TEXT_FIELDS:
        setattr(offer, field, _edited(getattr(offer, field), _clean_text(field, values[field])))
    for field in OFFER_LIST_FIELDS:
        existing = {value.text.casefold(): value for value in getattr(offer, field)}
        setattr(
            offer,
            field,
            [
                existing.get(text.casefold()) or FieldValue(text=text, edited=True)
                for text in _clean_values(field, values[field])
            ],
        )
    if not offer.name.text:
        raise ReviewError("An offer needs a name.")


def _find_offer(content: GeneratedContent, offer_id: str) -> Offer:
    for offer in content.offers:
        if offer.id == offer_id:
            return offer
    raise ReviewError("This offer no longer exists. Reload the page.")


def _find_question(content: GeneratedContent, ref: str) -> Question:
    for question in content.questions:
        if question.ref == ref:
            return question
    raise ReviewError("This question no longer exists. Reload the page.")


def update_offer(content: GeneratedContent, offer_id: str, values: dict[str, str]) -> None:
    """Apply reviewer form values to an existing offer.

    Changed text is flagged as edited and keeps the original evidence; list values are one
    per line, and unchanged list values keep their evidence.

    Args:
        content: Content to change in place.
        offer_id: ID of the offer to change.
        values: Raw form text per offer field in `OFFER_TEXT_FIELDS` and `OFFER_LIST_FIELDS`.

    Raises:
        ReviewError: If the offer no longer exists, the name is empty, or an email address
            or web address is invalid.
    """
    _apply_offer(_find_offer(content, offer_id), values)


def add_offer(content: GeneratedContent, values: dict[str, str]) -> Offer:
    """Add a reviewer-written offer; all of its values are flagged as edited.

    Args:
        content: Content to change in place.
        values: Raw form text per offer field in `OFFER_TEXT_FIELDS` and `OFFER_LIST_FIELDS`.

    Returns:
        The new offer.

    Raises:
        ReviewError: If the name is empty or an email address or web address is invalid.
    """
    offer = Offer()
    _apply_offer(offer, values)
    content.offers.append(offer)
    return offer


def remove_offer(content: GeneratedContent, offer_id: str) -> None:
    """Remove an offer.

    Args:
        content: Content to change in place.
        offer_id: ID of the offer to remove.

    Raises:
        ReviewError: If the offer no longer exists.
    """
    content.offers.remove(_find_offer(content, offer_id))


def _question_values(question: str, answer: str) -> tuple[str, str]:
    question_text = clean_line(question)[:MAX_QUESTION_LENGTH]
    answer_text = clean_markdown(answer)[:MAX_FIELD_LENGTH]
    if not question_text or not answer_text:
        raise ReviewError("A question needs both the question and an answer.")
    return question_text, answer_text


def _set_parent(content: GeneratedContent, question: Question, parent_ref: str) -> None:
    if parent_ref:
        _find_question(content, parent_ref)
    previous = question.parent_ref
    question.parent_ref = parent_ref
    by_ref = {item.ref: item for item in content.questions}
    if parent_ref and has_cycle(question, by_ref):
        question.parent_ref = previous
        raise ReviewError("A question cannot be nested under itself or its own follow-ups.")


def update_question(
    content: GeneratedContent, ref: str, question: str, answer: str, parent_ref: str
) -> None:
    """Apply reviewer edits to a question, its answer and its parent.

    Args:
        content: Content to change in place.
        ref: Ref of the question to change.
        question: New question text.
        answer: New answer text (Markdown).
        parent_ref: Ref of the new parent question, or "" for a top-level question.

    Raises:
        ReviewError: If the question or parent no longer exists, the question or answer is
            empty, or the new parent would create a cycle.
    """
    item = _find_question(content, ref)
    question_text, answer_text = _question_values(question, answer)
    if question_text != item.question:
        item.question = question_text
        item.question_edited = True
    item.answer = _edited(item.answer, answer_text)
    _set_parent(content, item, parent_ref)


def add_question(content: GeneratedContent, question: str, answer: str, parent_ref: str) -> str:
    """Add a reviewer-written question; its question and answer are flagged as edited.

    Args:
        content: Content to change in place.
        question: Question text.
        answer: Answer text (Markdown).
        parent_ref: Ref of the parent question, or "" for a top-level question.

    Returns:
        The ref of the new question.

    Raises:
        ReviewError: If the question or answer is empty or the parent does not exist.
    """
    question_text, answer_text = _question_values(question, answer)
    item = Question(
        ref=f"r{secrets.token_hex(4)}",
        question=question_text,
        answer=FieldValue(text=answer_text, edited=True),
        question_edited=True,
    )
    content.questions.append(item)
    try:
        _set_parent(content, item, parent_ref)
    except ReviewError:
        content.questions.remove(item)
        raise
    return item.ref


def remove_question(content: GeneratedContent, ref: str) -> None:
    """Remove a question and move its follow-ups up to its own parent.

    Args:
        content: Content to change in place.
        ref: Ref of the question to remove.

    Raises:
        ReviewError: If the question no longer exists.
    """
    item = _find_question(content, ref)
    for child in content.questions:
        if child.parent_ref == ref:
            child.parent_ref = item.parent_ref
    content.questions.remove(item)


@dataclass
class Gap:
    """One thing a reviewer should check or follow up before or after publishing.

    Attributes:
        id: Stable gap ID, used as the key for the reviewer's status and notes.
        group: Heading to group gaps under: the sub-category name or "Sources and
            conversion".
        subcategory_key: Key of the affected sub-category, or "" for source gaps.
        subcategory_id: HIA #ID of the affected sub-category, or None for source gaps.
        subcategory: Name of the affected sub-category, or "" for source gaps.
        item_type: "Offer", "Q&A", "Sub-category", "Source", "Item" or "".
        item: Offer ID and slug, question slug, source reference or the model's item name.
        field: Affected field, if known.
        kind: Issue kind, e.g. "missing", "unverified", "empty" or "conversion".
        issue: Description of the problem.
        source_reference: References of the related source passages.
        suggested_action: Reviewer's action, or the default action for the kind.
        suggested_contact: Reviewer's suggested contact.
        status: One of the `GAP_STATUSES` keys; "open" until a reviewer changes it.
    """

    id: str
    group: str
    subcategory_key: str
    subcategory_id: int | None
    subcategory: str
    item_type: str
    item: str
    field: str
    kind: str
    issue: str
    source_reference: str
    suggested_action: str
    suggested_contact: str
    status: str

    @property
    def kind_label(self) -> str:
        """Human-readable label of the issue kind."""
        return GAP_KIND_LABELS.get(self.kind, self.kind)

    @property
    def status_label(self) -> str:
        """Human-readable label of the status."""
        return GAP_STATUSES.get(self.status, self.status)


def evidence_reference(evidence: list[Evidence]) -> str:
    """Format evidence as "S1 p.3 (chunk ID)" references, separated by semicolons."""
    parts = []
    for item in evidence:
        location = ""
        if item.page:
            location = f" sheet {item.sheet}" if item.sheet else f" p.{item.page}"
        parts.append(f"{item.source_ref}{location} ({item.chunk_id})")
    return "; ".join(parts)


class GapStore:
    """Store the reviewer's status, action and contact per gap in the session's table rows.

    Args:
        table: Table store; gap rows are keyed by `GAP_ROW_PREFIX` plus the gap ID.
    """

    def __init__(self, table: TableStore) -> None:
        self._table = table

    def overrides(self, session_key: str) -> dict[str, dict[str, Any]]:
        """Return the stored reviewer values per gap ID for a session."""
        return {
            row.removeprefix(GAP_ROW_PREFIX): entity.data
            for row, entity in self._table.list_partition(session_key)
            if row.startswith(GAP_ROW_PREFIX)
        }

    def update(
        self, session_key: str, gap_id: str, *, status: str, action: str, contact: str
    ) -> None:
        """Save the reviewer's status, action and contact for one gap.

        Args:
            session_key: Key of the workspace session.
            gap_id: ID of the gap.
            status: One of the `GAP_STATUSES` keys.
            action: Suggested action, including any contact; trimmed to 1000 characters.
            contact: Suggested contact; trimmed to 200 characters.

        Raises:
            ReviewError: If the status is not valid.
        """
        if status not in GAP_STATUSES:
            raise ReviewError("Choose a valid status.")

        def apply(data: dict[str, Any]) -> bool:
            data.update(status=status, action=action.strip()[:1000], contact=contact.strip()[:200])
            return True

        mutate_entity(self._table, session_key, f"{GAP_ROW_PREFIX}{gap_id}", apply, create=True)


def collect_gaps(
    categories: list[AssembledCategory],
    sources: list[Source],
    overrides: dict[str, dict[str, Any]],
) -> list[Gap]:
    """Collect the gaps of a draft, merged with the reviewer's stored values.

    Gaps come from content issues, empty sub-categories, and sources that failed or were
    cancelled or have conversion limitations. Facts in failed sources are unavailable, not
    missing.

    Args:
        categories: Assembled categories with their content.
        sources: Sources of the session.
        overrides: Reviewer values per gap ID, from `GapStore.overrides`.

    Returns:
        Gaps in sub-category order, followed by source gaps.
    """
    gaps: list[Gap] = []

    def add(gap_id: str, sub: AssembledSubcategory | None, **values: str) -> None:
        override = overrides.get(gap_id, {})
        kind = values["kind"]
        action = override.get("action", SUGGESTED_ACTIONS.get(kind, ""))
        contact = override.get("contact", "")
        if contact and contact.casefold() not in action.casefold():
            action = f"{action}\nContact: {contact}"
        gaps.append(
            Gap(
                id=gap_id,
                group=sub.name if sub else "Sources and conversion",
                subcategory_key=sub.key if sub else "",
                subcategory_id=sub.id if sub else None,
                subcategory=sub.name if sub else "",
                item_type=values.get("item_type", ""),
                item=values.get("item", ""),
                field=values.get("field", ""),
                kind=kind,
                issue=values["issue"],
                source_reference=values.get("source_reference", ""),
                suggested_action=action,
                suggested_contact=override.get("contact", ""),
                status=override.get("status", "open"),
            )
        )

    for category in categories:
        for sub in category.subcategories:
            content = sub.content
            if content is None:
                continue
            if content.is_empty:
                add(
                    f"empty-{sub.key}",
                    sub,
                    kind="empty",
                    item_type="Sub-category",
                    item=sub.slug,
                    issue="No offers or questions are supported by the sources.",
                )
            for issue in content.issues:
                item_type, item = sub.item_label(issue.item_ref)
                add(
                    issue.id,
                    sub,
                    kind=issue.kind,
                    item_type=item_type if issue.item_ref else ("Item" if issue.item else ""),
                    item=item or issue.item,
                    field=issue.field,
                    issue=issue.description,
                    source_reference=evidence_reference(issue.evidence),
                )

    for source in sources:
        if source.status in (SourceStatus.FAILED, SourceStatus.CANCELLED):
            add(
                f"src-{source.id}-e",
                None,
                kind="conversion",
                item_type="Source",
                item=source.reference,
                issue=f"{source.title}: {source.error_message or 'Not converted.'} "
                "Facts in this source are unavailable, not missing.",
                source_reference=source.reference,
            )
        for number, limitation in enumerate(source.limitations, start=1):
            add(
                f"src-{source.id}-{number}",
                None,
                kind="conversion",
                item_type="Source",
                item=source.reference,
                issue=f"{source.title}: {limitation}",
                source_reference=source.reference,
            )
    return gaps


ContentChange = Callable[[GeneratedContent], Any]
