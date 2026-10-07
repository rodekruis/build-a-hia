"""The HIA structure: categories containing sub-categories, plus pure edit operations.

The structure has exactly two levels, so cycles cannot occur; every edit validates that
keys exist and that sub-categories stay inside a category.
"""

import copy
import json
import secrets
from dataclasses import asdict, dataclass, field

MAX_CATEGORIES = 15
MAX_SUBCATEGORIES = 15
MAX_NAME_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 1000


class StructureError(Exception):
    """An edit that would make the structure invalid.

    Args:
        message: User-facing explanation, safe to show in the UI.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def new_key() -> str:
    """Return a new random key for a structure node."""
    return secrets.token_hex(4)


def clean_name(value: str) -> str:
    """Collapse whitespace in a node name and truncate it to the maximum length."""
    return " ".join(value.split())[:MAX_NAME_LENGTH]


def clean_description(value: str) -> str:
    """Strip a node description and truncate it to the maximum length."""
    return value.strip()[:MAX_DESCRIPTION_LENGTH]


def _name_key(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass
class Node:
    """A category or sub-category in the structure.

    Attributes:
        key: Stable random identifier, used in URLs, forms and job messages.
        name: Public name; must be unique within its level for the HIA template.
        description: Optional public one-sentence description.
        chunk_ids: Labels of source passages linked to a sub-category.
        children: Sub-categories of a category; always empty for a sub-category.
    """

    key: str
    name: str
    description: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        """Build a node and its children from their JSON representation."""
        return cls(
            key=data["key"],
            name=data["name"],
            description=data.get("description", ""),
            chunk_ids=list(data.get("chunk_ids", [])),
            children=[cls.from_dict(child) for child in data.get("children", [])],
        )


@dataclass
class Structure:
    """The two-level HIA structure: an ordered list of categories with sub-categories."""

    categories: list[Node] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialize the structure to JSON."""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Structure":
        """Load a structure serialized with `to_json`."""
        data = json.loads(raw)
        return cls(categories=[Node.from_dict(item) for item in data.get("categories", [])])

    def copy(self) -> "Structure":
        """Return a deep copy that can be edited without affecting this structure."""
        return copy.deepcopy(self)

    def subcategories(self) -> list[tuple[Node, Node]]:
        """Return all ``(category, sub-category)`` pairs in display order."""
        return [(category, sub) for category in self.categories for sub in category.children]

    def find(self, key: str) -> tuple[Node | None, Node]:
        """Find a category or sub-category by key.

        Args:
            key: Key of the node to find.

        Returns:
            A ``(parent, node)`` tuple; ``parent`` is None when the node is a category.

        Raises:
            StructureError: If no node has this key.
        """
        for category in self.categories:
            if category.key == key:
                return None, category
            for sub in category.children:
                if sub.key == key:
                    return category, sub
        raise StructureError("This item no longer exists. Reload the page.")

    def duplicate_names(self) -> list[str]:
        """Return names used more than once among categories or among sub-categories.

        Names are compared case-insensitively with whitespace collapsed; each duplicate is
        reported once, as first spelled.
        """
        duplicates: list[str] = []
        for level in (
            [c.name for c in self.categories],
            [s.name for _c, s in self.subcategories()],
        ):
            seen: dict[str, str] = {}
            for name in level:
                key = _name_key(name)
                if key in seen and name not in duplicates:
                    duplicates.append(name)
                seen.setdefault(key, name)
        return duplicates

    def problems(self) -> list[str]:
        """Return user-facing reasons why the structure cannot be approved yet.

        Returns:
            Messages for a missing category, empty categories and duplicate names; empty if
            the structure can be approved.
        """
        problems = []
        if not self.categories:
            problems.append("Add at least one category.")
        for category in self.categories:
            if not category.children:
                problems.append(f"Category “{category.name}” has no sub-categories.")
        for name in self.duplicate_names():
            problems.append(
                f"The name “{name}” is used more than once. Category names and sub-category "
                "names must each be unique, because the HIA template links items by name."
            )
        return problems


def _check_name(name: str) -> str:
    cleaned = clean_name(name)
    if not cleaned:
        raise StructureError("Enter a name.")
    return cleaned


def rename(structure: Structure, key: str, name: str, description: str) -> None:
    """Change the name and description of a node in place.

    Args:
        structure: Structure to edit.
        key: Key of the category or sub-category.
        name: New name; whitespace is collapsed and it is truncated to the maximum length.
        description: New description; truncated to the maximum length.

    Raises:
        StructureError: If the node does not exist or the name is empty.
    """
    _parent, node = structure.find(key)
    node.name = _check_name(name)
    node.description = clean_description(description)


def add_category(structure: Structure, name: str, description: str) -> Node:
    """Append a new, empty category.

    Args:
        structure: Structure to edit.
        name: Name of the category.
        description: Optional description of the category.

    Returns:
        The new category.

    Raises:
        StructureError: If the name is empty or the maximum number of categories is reached.
    """
    if len(structure.categories) >= MAX_CATEGORIES:
        raise StructureError(f"A structure can have at most {MAX_CATEGORIES} categories.")
    node = Node(key=new_key(), name=_check_name(name), description=clean_description(description))
    structure.categories.append(node)
    return node


def add_subcategory(structure: Structure, category_key: str, name: str, description: str) -> Node:
    """Append a new sub-category to a category.

    Args:
        structure: Structure to edit.
        category_key: Key of the category to add to.
        name: Name of the sub-category.
        description: Optional description of the sub-category.

    Returns:
        The new sub-category.

    Raises:
        StructureError: If the category does not exist or is a sub-category, the name is
            empty, or the category already has the maximum number of sub-categories.
    """
    parent, category = structure.find(category_key)
    if parent is not None:
        raise StructureError("Sub-categories can only be added to a category.")
    if len(category.children) >= MAX_SUBCATEGORIES:
        raise StructureError(f"A category can have at most {MAX_SUBCATEGORIES} sub-categories.")
    node = Node(key=new_key(), name=_check_name(name), description=clean_description(description))
    category.children.append(node)
    return node


def remove(structure: Structure, key: str) -> None:
    """Remove a category (with its sub-categories) or a sub-category.

    Raises:
        StructureError: If the node does not exist.
    """
    parent, node = structure.find(key)
    siblings = structure.categories if parent is None else parent.children
    siblings.remove(node)


def move(structure: Structure, key: str, offset: int) -> None:
    """Swap a node with a sibling at a relative position.

    Moves that would go past either end of the list are ignored.

    Args:
        structure: Structure to edit.
        key: Key of the node to move.
        offset: Relative position of the sibling to swap with, e.g. -1 for up, 1 for down.

    Raises:
        StructureError: If the node does not exist.
    """
    parent, node = structure.find(key)
    siblings = structure.categories if parent is None else parent.children
    index = siblings.index(node)
    target = index + offset
    if 0 <= target < len(siblings):
        siblings[index], siblings[target] = siblings[target], siblings[index]


def change_parent(structure: Structure, key: str, category_key: str) -> None:
    """Move a sub-category to the end of another category.

    Moving to the current parent is a no-op.

    Args:
        structure: Structure to edit.
        key: Key of the sub-category to move.
        category_key: Key of the new parent category.

    Raises:
        StructureError: If either node does not exist, ``key`` is a category,
            ``category_key`` is a sub-category, or the new parent is full.
    """
    parent, node = structure.find(key)
    if parent is None:
        raise StructureError("Only sub-categories can move to another category.")
    new_parent, category = structure.find(category_key)
    if new_parent is not None:
        raise StructureError("Choose a category as the new parent.")
    if category is parent:
        return
    if len(category.children) >= MAX_SUBCATEGORIES:
        raise StructureError(f"A category can have at most {MAX_SUBCATEGORIES} sub-categories.")
    parent.children.remove(node)
    category.children.append(node)


def merge_preview(structure: Structure, source_key: str, target_key: str) -> Node:
    """Return the target node as it would look after merging the source into it.

    The structure itself is not changed.

    Args:
        structure: Structure to preview the merge in.
        source_key: Key of the node that would be merged away.
        target_key: Key of the node that would remain.

    Returns:
        A copy of the merged target node.

    Raises:
        StructureError: If the merge is not allowed; see `merge`.
    """
    preview = structure.copy()
    merge(preview, source_key, target_key)
    return preview.find(target_key)[1]


def merge(structure: Structure, source_key: str, target_key: str) -> None:
    """Merge the source node into the target node and remove the source.

    Merging categories moves the source's sub-categories to the target. In both cases the
    source description is appended (if not already present, truncated to the maximum
    length) and passage labels are combined without duplicates.

    Args:
        structure: Structure to edit.
        source_key: Key of the node to merge away.
        target_key: Key of the node that remains.

    Raises:
        StructureError: If the keys are equal or do not exist, the nodes are at different
            levels, or merged categories would exceed the maximum number of sub-categories.
    """
    if source_key == target_key:
        raise StructureError("Choose two different items to merge.")
    source_parent, source = structure.find(source_key)
    target_parent, target = structure.find(target_key)
    if (source_parent is None) != (target_parent is None):
        raise StructureError("Only items at the same level can be merged.")
    if source_parent is None:
        if len(target.children) + len(source.children) > MAX_SUBCATEGORIES:
            raise StructureError(
                f"The merged category would have more than {MAX_SUBCATEGORIES} sub-categories."
            )
        target.children.extend(source.children)
        structure.categories.remove(source)
    else:
        source_parent.children.remove(source)
    if source.description and source.description not in target.description:
        target.description = clean_description(
            "\n\n".join(part for part in (target.description, source.description) if part)
        )
    target.chunk_ids = list(dict.fromkeys(target.chunk_ids + source.chunk_ids))
