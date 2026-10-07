import pytest

from build_a_hia.services.assembly import assemble
from build_a_hia.services.content import (
    Evidence,
    FieldValue,
    GeneratedContent,
    Issue,
    Offer,
    Question,
)
from build_a_hia.services.review import (
    ReviewError,
    add_offer,
    add_question,
    collect_gaps,
    remove_offer,
    remove_question,
    update_offer,
    update_question,
)
from build_a_hia.services.sources import Source, SourceKind, SourceStatus
from build_a_hia.services.structure import Node, Structure

EVIDENCE = Evidence(
    chunk_id="S1-p1-c1", source_id="x", source_ref="S1", source_title="Guide", page=1
)


def _offer_values(**overrides: str) -> dict[str, str]:
    values = {
        "name": "Night shelter",
        "description": "Free beds.",
        "phone_numbers": "+380 44 123 4567",
        "emails": "",
        "web_urls": "",
        "address": "",
        "open_weekdays": "",
        "open_weekend": "",
        "need_to_know": "",
        "more_info": "",
        "chapter": "",
    }
    values.update(overrides)
    return values


@pytest.fixture
def content() -> GeneratedContent:
    return GeneratedContent(
        offers=[
            Offer(
                id="o1",
                name=FieldValue(text="Night shelter", evidence=[EVIDENCE]),
                description=FieldValue(text="Free beds.", evidence=[EVIDENCE]),
                phone_numbers=[FieldValue(text="+380 44 123 4567", evidence=[EVIDENCE])],
            )
        ],
        questions=[
            Question(
                ref="q1", question="Who?", answer=FieldValue(text="All.", evidence=[EVIDENCE])
            ),
            Question(ref="q2", parent_ref="q1", question="When?", answer=FieldValue(text="Now.")),
        ],
        generated_on="2026-10-07",
        prompt_version="t",
    )


def test_editing_marks_changed_fields_and_keeps_original(content):
    update_offer(
        content,
        "o1",
        _offer_values(
            description="Free beds for everyone.", phone_numbers="+380 44 123 4567\n1545"
        ),
    )

    offer = content.offers[0]
    assert not offer.name.edited
    assert offer.description.edited
    assert offer.description.original == "Free beds."
    assert offer.description.evidence == [EVIDENCE]
    assert [(p.text, p.edited, bool(p.evidence)) for p in offer.phone_numbers] == [
        ("+380 44 123 4567", False, True),
        ("1545", True, False),
    ]

    update_offer(content, "o1", _offer_values(description="Third version."))
    assert content.offers[0].description.original == "Free beds."


def test_reverting_an_edit_clears_the_flag(content):
    update_offer(content, "o1", _offer_values(description="Changed."))
    update_offer(content, "o1", _offer_values())

    description = content.offers[0].description
    assert not description.edited
    assert description.evidence == [EVIDENCE]


def test_offer_values_are_validated(content):
    with pytest.raises(ReviewError):
        update_offer(content, "o1", _offer_values(emails="not-an-email"))
    with pytest.raises(ReviewError):
        update_offer(content, "o1", _offer_values(web_urls="javascript:alert(1)"))
    with pytest.raises(ReviewError):
        update_offer(content, "o1", _offer_values(name="  "))


def test_public_text_is_sanitized_on_edit(content):
    update_offer(content, "o1", _offer_values(description="<b>Free</b> [S1-p1-c1]\n# Beds"))

    assert content.offers[0].description.text == "Free\n### Beds"


def test_added_offers_are_marked_as_reviewer_content(content):
    offer = add_offer(content, _offer_values(name="Legal aid", phone_numbers=""))

    assert offer.name.edited and not offer.name.evidence
    remove_offer(content, offer.id)
    assert [item.id for item in content.offers] == ["o1"]


def test_questions_cannot_form_cycles(content):
    with pytest.raises(ReviewError):
        update_question(content, "q1", "Who?", "All.", "q2")

    ref = add_question(content, "How?", "Online.", "q2")
    assert content.questions[-1].parent_ref == "q2"
    assert content.questions[-1].question_edited
    update_question(content, ref, "How exactly?", "Online.", "")
    assert content.questions[-1].parent_ref == ""


def test_removing_a_question_keeps_its_follow_ups(content):
    remove_question(content, "q1")

    assert [(q.ref, q.parent_ref) for q in content.questions] == [("q2", "")]


def test_gaps_combine_issues_empty_content_and_conversion_problems(content):
    content.issues = [
        Issue(
            id="i1",
            kind="missing",
            item="Night shelter",
            item_ref="o1",
            field="address",
            description="No address.",
            evidence=[EVIDENCE],
        )
    ]
    structure = Structure(
        categories=[
            Node(
                key="c1",
                name="Shelter",
                children=[Node(key="s1", name="Beds"), Node(key="s2", name="Rent")],
            )
        ]
    )
    empty = GeneratedContent(generated_on="2026-10-07", prompt_version="t")
    categories = assemble(structure, {"s1": content, "s2": empty})
    sources = [
        Source(
            id="src1",
            number=2,
            kind=SourceKind.FILE,
            title="Scan",
            status=SourceStatus.FAILED,
            created_at="",
            error_message="No readable text was found.",
            limitations=["Pages 3-4 unreadable."],
        )
    ]

    gaps = collect_gaps(categories, sources, {"i1": {"status": "resolved", "contact": "Desk"}})

    by_id = {gap.id: gap for gap in gaps}
    assert by_id["i1"].item == "2 / night-shelter"
    assert by_id["i1"].item_type == "Offer"
    assert by_id["i1"].source_reference == "S1 p.1 (S1-p1-c1)"
    assert by_id["i1"].status == "resolved"
    assert by_id["i1"].suggested_contact == "Desk"
    assert by_id["i1"].suggested_action
    assert by_id["empty-s2"].kind == "empty"
    assert by_id["src-src1-e"].kind == "conversion"
    assert "unavailable, not missing" in by_id["src-src1-e"].issue
    assert by_id["src-src1-1"].issue == "Scan: Pages 3-4 unreadable."
