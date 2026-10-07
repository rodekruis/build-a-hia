import pytest

from build_a_hia.services.structure import (
    MAX_SUBCATEGORIES,
    Node,
    Structure,
    StructureError,
    add_category,
    add_subcategory,
    change_parent,
    merge,
    merge_preview,
    move,
    remove,
    rename,
)


@pytest.fixture
def structure() -> Structure:
    return Structure(
        categories=[
            Node(
                key="aaaaaaaa",
                name="Shelter",
                chunk_ids=["S1-p1-c1"],
                children=[
                    Node(key="a1a1a1a1", name="Night shelter"),
                    Node(key="a2a2a2a2", name="Rent support"),
                ],
            ),
            Node(key="bbbbbbbb", name="Health", children=[Node(key="b1b1b1b1", name="Doctors")]),
        ]
    )


def _names(structure: Structure) -> list[tuple[str, list[str]]]:
    return [(c.name, [s.name for s in c.children]) for c in structure.categories]


def test_json_round_trip(structure):
    assert Structure.from_json(structure.to_json()) == structure


def test_rename_cleans_whitespace(structure):
    rename(structure, "a1a1a1a1", "  Night   shelters ", "Beds for the night.")

    assert structure.find("a1a1a1a1")[1].name == "Night shelters"
    with pytest.raises(StructureError):
        rename(structure, "a1a1a1a1", "   ", "")


def test_add_and_remove(structure):
    category = add_category(structure, "Legal aid", "")
    sub = add_subcategory(structure, category.key, "Lawyers", "Free advice.")

    assert _names(structure)[-1] == ("Legal aid", ["Lawyers"])
    with pytest.raises(StructureError):
        add_subcategory(structure, sub.key, "Nested", "")
    remove(structure, category.key)
    assert [c.name for c in structure.categories] == ["Shelter", "Health"]


def test_move_reorders_within_level(structure):
    move(structure, "a2a2a2a2", -1)
    move(structure, "bbbbbbbb", -1)
    move(structure, "bbbbbbbb", -1)

    assert _names(structure) == [
        ("Health", ["Doctors"]),
        ("Shelter", ["Rent support", "Night shelter"]),
    ]


def test_change_parent_moves_sub_category_only(structure):
    change_parent(structure, "a2a2a2a2", "bbbbbbbb")

    assert _names(structure) == [
        ("Shelter", ["Night shelter"]),
        ("Health", ["Doctors", "Rent support"]),
    ]
    with pytest.raises(StructureError):
        change_parent(structure, "aaaaaaaa", "bbbbbbbb")
    with pytest.raises(StructureError):
        change_parent(structure, "a1a1a1a1", "b1b1b1b1")


def test_merging_categories_combines_children(structure):
    preview = merge_preview(structure, "bbbbbbbb", "aaaaaaaa")

    assert [child.name for child in preview.children] == [
        "Night shelter",
        "Rent support",
        "Doctors",
    ]
    assert len(structure.categories) == 2

    merge(structure, "bbbbbbbb", "aaaaaaaa")
    assert _names(structure) == [("Shelter", ["Night shelter", "Rent support", "Doctors"])]


def test_merging_sub_categories_keeps_target(structure):
    structure.find("a2a2a2a2")[1].chunk_ids = ["S1-p2-c1"]
    merge(structure, "a2a2a2a2", "a1a1a1a1")

    target = structure.find("a1a1a1a1")[1]
    assert _names(structure)[0] == ("Shelter", ["Night shelter"])
    assert target.chunk_ids == ["S1-p2-c1"]


def test_merge_rejects_mixed_levels_and_self(structure):
    with pytest.raises(StructureError):
        merge(structure, "a1a1a1a1", "bbbbbbbb")
    with pytest.raises(StructureError):
        merge(structure, "a1a1a1a1", "a1a1a1a1")


def test_limits_are_enforced(structure):
    for number in range(MAX_SUBCATEGORIES - 2):
        add_subcategory(structure, "aaaaaaaa", f"Sub {number}", "")

    with pytest.raises(StructureError):
        add_subcategory(structure, "aaaaaaaa", "One too many", "")


def test_unknown_key_is_reported(structure):
    with pytest.raises(StructureError):
        remove(structure, "deadbeef")


def test_duplicate_names_are_problems(structure):
    rename(structure, "b1b1b1b1", "night  SHELTER", "")
    rename(structure, "bbbbbbbb", "Shelter", "")

    assert structure.duplicate_names() == ["Shelter", "night SHELTER"]
    assert len(structure.problems()) == 2


def test_empty_category_is_a_problem(structure):
    remove(structure, "b1b1b1b1")

    assert structure.problems() == ["Category “Health” has no sub-categories."]
