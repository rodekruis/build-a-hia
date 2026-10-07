"""Step 8: check export readiness and produce both downloads from one draft snapshot."""

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .assembly import AssembledCategory, assemble
from .content import (
    FIELD_LABELS,
    OFFER_LIST_FIELDS,
    OFFER_TEXT_FIELDS,
    FieldValue,
    GeneratedContent,
)
from .drafting import ContentService, ContentState, StructureService, StructureState
from .review import Gap, GapStore, collect_gaps
from .sessions import WorkspaceSession, session_blob_prefix, utcnow
from .sources import Source, SourceRepository
from .storage import BlobStore, NotFoundError, TableStore, mutate_entity
from .structure import Structure
from .workbook import CONTRACT, build_hia_workbook, build_review_workbook

EXPORT_ROW = "export"
EXPORT_FILES = ("hia.xlsx", "review-internal.xlsx")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Phone numbers ("+31 …") and Markdown bullets ("- …") stay allowed.
_PASTE_FORMULA = re.compile(r"^\s*(?:=|[+-][A-Za-z(])")


class ExportError(Exception):
    """Raised when an export cannot be created or downloaded.

    Args:
        message: User-facing explanation, also kept in the `message` attribute.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def export_blob(session_key: str, snapshot: str, name: str) -> str:
    """Return the blob path of one export file within a session's snapshot folder."""
    return f"{session_blob_prefix(session_key)}exports/{snapshot}/{name}"


def content_status(
    state: ContentState | None, structure: StructureState, session: WorkspaceSession
) -> str:
    """Return the effective generation status of a sub-category's content.

    Args:
        state: Stored content state, or None when nothing was generated yet.
        structure: State of the approved structure the content must match.
        session: Workspace session holding the approved sources and context versions.

    Returns:
        "missing" when nothing was generated, "outdated" when finished content was
        generated for an older structure, sources or context, otherwise the stored status.
    """
    if state is None or not state.status:
        return "missing"
    if state.status == "done" and not state.is_current(structure, session):
        return "outdated"
    return state.status


@dataclass
class SubcategoryReview:
    """Review status of one sub-category of the approved structure.

    Attributes:
        key: Stable sub-category key from the structure.
        category: Name of the parent category.
        name: Sub-category name.
        status: Effective status from `content_status`.
        state: Stored content state, or None when nothing was generated yet.
        content: Current generated (possibly reviewer-edited) content, if any.
    """

    key: str
    category: str
    name: str
    status: str
    state: ContentState | None
    content: GeneratedContent | None

    @property
    def dropped(self) -> bool:
        """Whether the reviewer approved leaving this empty sub-category out."""
        return bool(self.state and self.state.empty_decision == "drop" and self.state.approved)

    @property
    def approved(self) -> bool:
        """Whether the current, up-to-date content is approved."""
        return bool(self.state and self.state.approved and self.status == "done")


@dataclass
class ExportPlan:
    """Everything needed to decide on and build an export of one session.

    Attributes:
        structure_state: State of the structure, approved or not.
        structure: The approved structure, or None when none is approved.
        subcategories: Review status per sub-category of the approved structure.
        categories: Assembled HIA rows, without the dropped sub-categories.
        gaps: Gaps with their reviewer status and notes.
        sources: Sources of the session.
        blockers: Reasons the export cannot be created yet.
        warnings: Problems that do not block the export.
    """

    structure_state: StructureState
    structure: Structure | None
    subcategories: list[SubcategoryReview]
    categories: list[AssembledCategory]
    gaps: list[Gap]
    sources: list[Source]
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Whether nothing blocks the export."""
        return not self.blockers

    @property
    def open_gaps(self) -> int:
        """Number of gaps that still have the "open" status."""
        return sum(gap.status == "open" for gap in self.gaps)

    @property
    def approved_keys(self) -> set[str]:
        """Keys of the sub-categories whose current content is approved."""
        return {sub.key for sub in self.subcategories if sub.approved}


class ExportService:
    """Check export readiness and store both downloads as one snapshot per session.

    Only the latest snapshot is kept; creating a new one deletes the previous files.

    Args:
        table: Table store for the export record, sources and gap overrides.
        blobs: Blob store for the exported files.
        structures: Service that provides the approved structure.
        contents: Service that provides the generated content per sub-category.
        clock: Returns the current UTC time; replaceable in tests.
    """

    def __init__(
        self,
        table: TableStore,
        blobs: BlobStore,
        structures: StructureService,
        contents: ContentService,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._table = table
        self._blobs = blobs
        self._sources = SourceRepository(table)
        self._structures = structures
        self._contents = contents
        self._gaps = GapStore(table)
        self._clock = clock

    def plan(self, session: WorkspaceSession) -> ExportPlan:
        """Assemble the current draft and list what blocks or warns about exporting it.

        Blockers include a missing context, unapproved sources, an unapproved structure,
        missing, running, failed, outdated or unapproved content, template capacity, invalid
        or duplicate slugs and IDs, and
        text that would run as a formula when pasted into Google Sheets.

        Args:
            session: The workspace session to export.

        Returns:
            The export plan; `ready` is true only when it has no blockers.
        """
        state, structure = self._structures.approved(session.key)
        states = self._contents.states(session.key)
        reviews: list[SubcategoryReview] = []
        contents: dict[str, GeneratedContent] = {}
        if structure is not None:
            for category, sub in structure.subcategories():
                sub_state = states.get(sub.key)
                result = self._contents.result(session.key, sub_state) if sub_state else None
                if result is not None:
                    contents[sub.key] = result
                reviews.append(
                    SubcategoryReview(
                        key=sub.key,
                        category=category.name,
                        name=sub.name,
                        status=content_status(sub_state, state, session),
                        state=sub_state,
                        content=result,
                    )
                )
        dropped = frozenset(review.key for review in reviews if review.dropped)
        categories = assemble(structure, contents, exclude=dropped) if structure else []
        sources = self._sources.list_for(session.key)
        plan = ExportPlan(
            structure_state=state,
            structure=structure,
            subcategories=reviews,
            categories=categories,
            gaps=collect_gaps(categories, sources, self._gaps.overrides(session.key)),
            sources=sources,
        )
        self._check(session, plan)
        return plan

    def _check(self, session: WorkspaceSession, plan: ExportPlan) -> None:
        blockers = plan.blockers
        if session.context is None:
            blockers.append("Describe the context first.")
        if not session.sources_approved:
            blockers.append("Approve the sources again: they changed after approval.")
        if plan.structure is None or not plan.structure_state.approved:
            blockers.append("Approve the structure.")
            return
        blockers += plan.structure.problems()
        labels = {
            "missing": "has no generated content yet",
            "queued": "is still being generated",
            "running": "is still being generated",
            "failed": "failed to generate; regenerate it",
            "outdated": "is outdated; regenerate it",
        }
        for sub in plan.subcategories:
            title = f"“{sub.category} › {sub.name}”"
            if sub.status in labels:
                blockers.append(f"{title} {labels[sub.status]}.")
            elif (
                sub.content is not None
                and sub.content.is_empty
                and not (sub.state and sub.state.empty_decision and sub.state.approved)
            ):
                blockers.append(f"{title} has no content: decide whether to keep or drop it.")
            elif not sub.approved:
                blockers.append(f"{title} needs review and approval.")
        counts = {
            "Categories": len(plan.categories),
            "Sub-Categories": sum(len(c.subcategories) for c in plan.categories),
            "Offers": sum(len(s.offers) for c in plan.categories for s in c.subcategories),
            "Q&As": sum(len(s.questions) for c in plan.categories for s in c.subcategories),
        }
        for sheet, count in counts.items():
            if count > CONTRACT[sheet].capacity:
                blockers.append(
                    f"The {sheet} tab supports {CONTRACT[sheet].capacity} rows; this draft has "
                    f"{count}. Merge or drop items."
                )
        blockers += _structural_errors(plan.categories)
        blockers += _formula_errors(plan.categories)
        for category in plan.categories:
            if not category.subcategories:
                plan.warnings.append(
                    f"Category “{category.name}” has no sub-categories after dropping empty ones."
                )

    def create_snapshot(self, session: WorkspaceSession) -> str:
        """Build both workbooks from one plan and store them as the latest snapshot.

        `hia.xlsx` is the publishable HIA workbook; `review-internal.xlsx` holds gaps,
        evidence and sources for internal review only.

        Args:
            session: The workspace session to export.

        Returns:
            The new snapshot ID.

        Raises:
            ExportError: If the plan has blockers or the context is missing.
            TemplateError: If the pinned HIA template does not match its contract.
        """
        plan = self.plan(session)
        if not plan.ready:
            raise ExportError(plan.blockers[0])
        if session.context is None:
            raise ExportError("Describe the context first.")
        snapshot = secrets.token_hex(6)
        exported_at = self._clock()
        files = {
            "hia.xlsx": build_hia_workbook(plan.categories, session.context, exported_at),
            "review-internal.xlsx": build_review_workbook(
                categories=plan.categories,
                gaps=plan.gaps,
                sources=plan.sources,
                approved=plan.approved_keys,
                snapshot=snapshot,
                exported_at=exported_at,
            ),
        }
        for name, data in files.items():
            self._blobs.put(export_blob(session.key, snapshot, name), data)
        previous = self.latest(session.key)

        def record(data: dict[str, Any]) -> bool:
            data.update(snapshot=snapshot, exported_at=exported_at.isoformat())
            return True

        mutate_entity(self._table, session.key, EXPORT_ROW, record, create=True)
        if previous:
            self._blobs.delete_prefix(
                f"{session_blob_prefix(session.key)}exports/{previous['snapshot']}/"
            )
        return snapshot

    def latest(self, session_key: str) -> dict[str, Any] | None:
        """Return the latest export record (`snapshot`, `exported_at`), if any."""
        stored = self._table.get(session_key, EXPORT_ROW)
        return stored.data if stored else None

    def file(self, session_key: str, snapshot: str, name: str) -> bytes:
        """Return the contents of one file of the latest snapshot.

        Args:
            session_key: Key of the workspace session.
            snapshot: Snapshot ID from the download link.
            name: One of `EXPORT_FILES`.

        Returns:
            The workbook bytes.

        Raises:
            ExportError: If the name is unknown, the snapshot is not the latest one, or the
                file no longer exists.
        """
        latest = self.latest(session_key)
        if name not in EXPORT_FILES or not latest or latest.get("snapshot") != snapshot:
            raise ExportError("This download is no longer available. Create a new one.")
        try:
            return self._blobs.get(export_blob(session_key, snapshot, name))
        except NotFoundError as error:
            raise ExportError("This download is no longer available.") from error


def _structural_errors(categories: list[AssembledCategory]) -> list[str]:
    errors: list[str] = []
    slugs: dict[str, set[str]] = {}
    ids: dict[str, set[int]] = {}

    def check(kind: str, slug: str, identifier: int | None = None) -> None:
        if not _SLUG.match(slug):
            errors.append(f"Invalid {kind} slug “{slug}”.")
        if slug in slugs.setdefault(kind, set()):
            errors.append(f"Duplicate {kind} slug “{slug}”.")
        slugs[kind].add(slug)
        if identifier is not None:
            if identifier in ids.setdefault(kind, set()):
                errors.append(f"Duplicate {kind} ID {identifier}.")
            ids[kind].add(identifier)

    for category in categories:
        check("category", category.slug, category.id)
        for sub in category.subcategories:
            check("sub-category", sub.slug, sub.id)
            for offer in sub.offers:
                check("offer", offer.slug, offer.id)
            question_slugs = {question.slug for question in sub.questions}
            for question in sub.questions:
                check("question", question.slug)
                if question.parent_slug and question.parent_slug not in question_slugs:
                    errors.append(f"Question “{question.slug}” has an unknown parent.")
    return errors


def _formula_errors(categories: list[AssembledCategory]) -> list[str]:
    """Find text that Google Sheets would run as a formula when it is pasted as plain text.

    The workbook itself stores every value as text; this guards the copy-paste step.

    Args:
        categories: The assembled rows that go into `hia.xlsx`.

    Returns:
        One blocker message per affected value.
    """
    errors: list[str] = []

    def check(where: str, text: str) -> None:
        if _PASTE_FORMULA.match(text):
            errors.append(
                f"{where} starts like a formula (“=”, or “+”/“-” before a letter), so "
                "it would run when pasted into Google Sheets. Edit it."
            )

    for category in categories:
        for text in (category.name, category.description):
            check(f"Category “{category.slug}”", text)
        for sub in category.subcategories:
            for text in (sub.name, sub.description):
                check(f"Sub-category “{sub.slug}”", text)
            for item in sub.offers:
                for name in OFFER_TEXT_FIELDS + OFFER_LIST_FIELDS:
                    value: FieldValue | list[FieldValue] = getattr(item.offer, name)
                    for entry in value if isinstance(value, list) else [value]:
                        check(f"Offer {item.id} ({FIELD_LABELS[name]})", entry.text)
            for question in sub.questions:
                check(f"Q&A “{question.slug}”", question.question.question)
                check(f"Q&A “{question.slug}”", question.question.answer.text)
    return errors
