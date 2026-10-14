"""Assemble the approved structure and generated content into HIA rows.

Code, not the model, owns IDs, slugs and links: #ID values are sequential per entity type,
slugs are unique per entity type and limited to "a-z 0-9 -" as the template requires, and
nested questions point to their parent's slug in #PARENT.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from .content import GeneratedContent, Offer, Question
from .structure import Structure

VISIBLE = "Show"
HIGHLIGHT = "No"
MAX_SLUG_LENGTH = 60
# Keep generated IDs compatible with exports that included the demo scaffolding row.
FIRST_ID = 2
# Keep the former demo scaffolding slugs reserved for compatibility.
RESERVED_SLUGS = frozenset({"hidden", "example-question"})


def slugify(text: str) -> str:
    """Return an ASCII slug of "a-z 0-9 -", at most `MAX_SLUG_LENGTH` characters; may be ""."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:MAX_SLUG_LENGTH].rstrip("-")


class SlugRegistry:
    """Hand out unique slugs for one entity type, never reusing the template's reserved ones.

    Args:
        prefix: Fallback slug prefix for text without usable characters, e.g. "offer".
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._used: set[str] = set(RESERVED_SLUGS)

    def claim(self, text: str, number: int) -> str:
        """Reserve and return a unique slug for the text.

        Args:
            text: Name or question to derive the slug from.
            number: Number for the fallback slug "<prefix>-<number>".

        Returns:
            The slug, with "-2", "-3", ... appended when it is already taken.
        """
        base = slugify(text) or f"{self._prefix}-{number}"
        slug = base
        suffix = 2
        while slug in self._used:
            slug = f"{base}-{suffix}"
            suffix += 1
        self._used.add(slug)
        return slug


@dataclass
class AssembledOffer:
    """An offer row with its HIA #ID and #SLUG."""

    id: int
    slug: str
    offer: Offer


@dataclass
class AssembledQuestion:
    """A Q&A row.

    Attributes:
        slug: HIA #SLUG.
        parent_slug: #PARENT: slug of the parent question, or "" for a top-level question.
        question: The validated question.
        updated: #UPDATED date: last edit date, or generation date.
        depth: Nesting level; 0 for top-level questions.
    """

    slug: str
    parent_slug: str
    question: Question
    updated: str
    depth: int


@dataclass
class AssembledSubcategory:
    """A sub-category row with its offers and Q&As.

    Attributes:
        key: Stable sub-category key from the structure.
        id: HIA #ID.
        slug: HIA #SLUG.
        name: Sub-category name.
        description: Sub-category description.
        category_id: HIA #ID of the parent category.
        content: Generated content, or None when there is none.
        offers: Offer rows.
        questions: Q&A rows, parents first, each followed by its follow-ups.
    """

    key: str
    id: int
    slug: str
    name: str
    description: str
    category_id: int
    content: GeneratedContent | None
    offers: list[AssembledOffer] = field(default_factory=list)
    questions: list[AssembledQuestion] = field(default_factory=list)

    def item_label(self, item_ref: str) -> tuple[str, str]:
        """Return the item type and its "ID / slug" or slug for an offer id or question ref.

        Args:
            item_ref: Offer id or question ref.

        Returns:
            ("Offer", "<id> / <slug>"), ("Q&A", "<slug>"), or ("Sub-category", "") when the
            item is not found.
        """
        for offer in self.offers:
            if offer.offer.id == item_ref:
                return "Offer", f"{offer.id} / {offer.slug}"
        for question in self.questions:
            if question.question.ref == item_ref:
                return "Q&A", question.slug
        return "Sub-category", ""


@dataclass
class AssembledCategory:
    """A category row with its sub-categories.

    Attributes:
        key: Stable category key from the structure.
        id: HIA #ID.
        slug: HIA #SLUG.
        name: Category name.
        description: Category description.
        subcategories: Sub-category rows.
    """

    key: str
    id: int
    slug: str
    name: str
    description: str
    subcategories: list[AssembledSubcategory] = field(default_factory=list)


def assemble(
    structure: Structure,
    contents: dict[str, GeneratedContent],
    *,
    exclude: frozenset[str] = frozenset(),
) -> list[AssembledCategory]:
    """Turn a structure and its content into HIA rows with IDs, slugs and parent links.

    IDs are sequential per entity type in structure order and start at `FIRST_ID` (2)
    for compatibility with earlier exports. Excluded sub-categories get no ID, and their
    content is left out.

    Args:
        structure: The approved structure.
        contents: Generated content by sub-category key; missing keys give empty rows.
        exclude: Keys of sub-categories to leave out, e.g. dropped empty ones.

    Returns:
        Categories in structure order, each with its sub-categories, offers and Q&As.
    """
    category_slugs = SlugRegistry("category")
    subcategory_slugs = SlugRegistry("subcategory")
    offer_slugs = SlugRegistry("offer")
    question_slugs = SlugRegistry("question")
    offer_id = FIRST_ID - 1
    subcategory_id = FIRST_ID - 1
    question_number = 0
    categories: list[AssembledCategory] = []

    for category_id, node in enumerate(structure.categories, start=FIRST_ID):
        category = AssembledCategory(
            key=node.key,
            id=category_id,
            slug=category_slugs.claim(node.name, category_id),
            name=node.name,
            description=node.description,
        )
        for sub in node.children:
            if sub.key in exclude:
                continue
            subcategory_id += 1
            content = contents.get(sub.key)
            assembled = AssembledSubcategory(
                key=sub.key,
                id=subcategory_id,
                slug=subcategory_slugs.claim(sub.name, subcategory_id),
                name=sub.name,
                description=sub.description,
                category_id=category_id,
                content=content,
            )
            if content is not None:
                for offer in content.offers:
                    offer_id += 1
                    slug = offer_slugs.claim(offer.name.text, offer_id)
                    assembled.offers.append(AssembledOffer(id=offer_id, slug=slug, offer=offer))
                slugs: dict[str, str] = {}
                for question in content.questions:
                    question_number += 1
                    slugs[question.ref] = question_slugs.claim(question.question, question_number)
                for question in _ordered(content.questions):
                    assembled.questions.append(
                        AssembledQuestion(
                            slug=slugs[question.ref],
                            parent_slug=slugs.get(question.parent_ref, ""),
                            question=question,
                            updated=content.updated_on or content.generated_on,
                            depth=_depth(question, content.questions),
                        )
                    )
            category.subcategories.append(assembled)
        categories.append(category)
    return categories


def _ordered(questions: list[Question]) -> list[Question]:
    """Parents first, each followed by its children, keeping the original order."""
    children: dict[str, list[Question]] = {}
    for question in questions:
        children.setdefault(question.parent_ref, []).append(question)
    ordered: list[Question] = []

    def visit(parent_ref: str) -> None:
        for question in children.get(parent_ref, []):
            ordered.append(question)
            visit(question.ref)

    visit("")
    return ordered


def _depth(question: Question, questions: list[Question]) -> int:
    by_ref = {item.ref: item for item in questions}
    depth = 0
    current = question
    while current.parent_ref and current.parent_ref in by_ref and depth < len(questions):
        depth += 1
        current = by_ref[current.parent_ref]
    return depth
