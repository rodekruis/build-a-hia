from build_a_hia.services.assembly import assemble, slugify
from build_a_hia.services.content import FieldValue, GeneratedContent, Offer, Question
from build_a_hia.services.structure import Node, Structure


def test_slugs_use_only_template_characters():
    assert slugify("Café & Shelter: 24/7!") == "cafe-shelter-24-7"
    assert slugify("Притулок") == ""


def _content(offers: list[str], questions: list[tuple[str, str, str]]) -> GeneratedContent:
    return GeneratedContent(
        offers=[Offer(name=FieldValue(text=name)) for name in offers],
        questions=[
            Question(ref=ref, parent_ref=parent, question=text, answer=FieldValue(text="Yes."))
            for ref, text, parent in questions
        ],
        generated_on="2026-10-07",
        prompt_version="t",
    )


def test_ids_and_slugs_are_assigned_by_code():
    structure = Structure(
        categories=[
            Node(
                key="c1",
                name="Shelter",
                children=[Node(key="s1", name="Beds"), Node(key="s2", name="Beds ")],
            ),
            Node(key="c2", name="Притулок", children=[Node(key="s3", name="Ліжка")]),
        ]
    )
    contents = {
        "s1": _content(["Night shelter", "Night shelter"], []),
        "s3": _content(
            ["Нічліжка"],
            [("q2", "Follow-up?", "q1"), ("q1", "Who can stay?", "")],
        ),
    }

    shelter, other = assemble(structure, contents)

    # IDs start at 2: the template's hidden scaffolding row 2 has ID 1.
    assert (shelter.id, shelter.slug, other.id, other.slug) == (2, "shelter", 3, "category-3")
    assert [(sub.id, sub.slug, sub.category_id) for sub in shelter.subcategories] == [
        (2, "beds", 2),
        (3, "beds-2", 2),
    ]
    beds, empty = shelter.subcategories
    assert [(item.id, item.slug) for item in beds.offers] == [
        (2, "night-shelter"),
        (3, "night-shelter-2"),
    ]
    assert empty.content is None and empty.offers == []
    [sub3] = other.subcategories
    assert (sub3.id, sub3.slug) == (4, "subcategory-4")
    assert [(item.id, item.slug) for item in sub3.offers] == [(4, "offer-4")]
    assert [(q.question.ref, q.slug, q.parent_slug, q.depth) for q in sub3.questions] == [
        ("q1", "who-can-stay", "", 0),
        ("q2", "follow-up", "who-can-stay", 1),
    ]
    assert sub3.questions[0].updated == "2026-10-07"


def test_scaffolding_slugs_are_reserved_and_dropped_items_excluded():
    structure = Structure(
        categories=[
            Node(
                key="c1",
                name="Hidden",
                children=[Node(key="s1", name="A"), Node(key="s2", name="B")],
            )
        ]
    )
    contents = {"s1": _content([], [("q1", "Example question", "")])}

    [category] = assemble(structure, contents, exclude=frozenset({"s2"}))

    assert category.slug == "hidden-2"
    assert [sub.key for sub in category.subcategories] == ["s1"]
    assert category.subcategories[0].questions[0].slug == "example-question-2"
