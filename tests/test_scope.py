import pytest
from factories import cited, offer, question

from build_a_hia.services.checks import ChunkIndex, ChunkRef, check_draft
from build_a_hia.services.content import SubcategoryDraft
from build_a_hia.services.scope import (
    ScopeReview,
    ScopeReviewError,
    apply_scope_review,
    scope_values,
)


def _draft():
    return SubcategoryDraft.model_validate(
        {
            "offers": [
                offer(
                    cited("YŞEY", "S1-p1-c1"),
                    need_to_know=cited("250 TL monthly per person.", "S1-p2-c1"),
                    phone_numbers=[cited("168", "S1-p2-c1"), cited("999", "S1-p2-c1")],
                ),
                offer(cited("T-SUY Projesi", "S1-p2-c1")),
            ],
            "questions": [
                question("q1", "ما قيمة المساعدة؟", cited("250 ليرة شهرياً.", "S1-p2-c1")),
                question("q2", "Where can I ask?", cited("Call 168.", "S1-p2-c1"), "q1"),
            ],
            "issues": [
                {
                    "kind": "conflict",
                    "item": "YŞEY",
                    "field": "need_to_know",
                    "description": "The amount belongs to T-SUY, not YŞEY.",
                    "chunk_ids": ["S1-p2-c1"],
                }
            ],
        }
    )


def _review(draft, **rejected):
    return ScopeReview.model_validate(
        {
            "verdicts": [
                {
                    "value_id": value.value_id,
                    "verdict": rejected.get(value.value_id, "applies"),
                    "reason": "The cited amount/service is for T-SUY, not YŞEY.",
                }
                for value in scope_values(draft)
            ]
        }
    )


def _checked(draft):
    index = ChunkIndex(
        [
            ChunkRef("S1-p1-c1", "s1", "S1", "YŞEY FAQ", 1, "", "YŞEY supports school attendance."),
            ChunkRef(
                "S1-p2-c1",
                "s1",
                "S1",
                "YŞEY FAQ",
                2,
                "",
                "T-SUY Projesi pays 250 TL monthly per person. Call 999 for T-SUY. "
                "Call 168 for questions about both YŞEY and T-SUY.",
            ),
        ]
    )
    return check_draft(draft, index, generated_on="2026-10-07", prompt_version="t")


def test_wrong_programme_facts_are_withheld_despite_valid_citations_and_conflict_issue():
    draft = _draft()
    review = _review(
        draft,
        **{
            "o0.need_to_know": "other_programme",
            "o0.phone_numbers.1": "other_programme",
            "o1.name": "other_programme",
            "q0.answer": "other_programme",
        },
    )

    content = _checked(apply_scope_review(draft, review))

    assert [item.name.text for item in content.offers] == ["YŞEY"]
    assert content.offers[0].need_to_know.text == ""
    assert [item.text for item in content.offers[0].phone_numbers] == ["168"]
    assert [item.ref for item in content.questions] == ["q2"]
    assert content.questions[0].parent_ref == ""
    gaps = [issue for issue in content.issues if issue.kind == "wrong_programme"]
    assert len(gaps) == 4
    assert all(issue.evidence[0].chunk_id == "S1-p2-c1" for issue in gaps)
    assert gaps[0].item_ref == content.offers[0].id
    assert draft.offers[0].need_to_know.text == "250 TL monthly per person."


def test_uncertain_scope_is_also_withheld():
    draft = _draft()
    content = _checked(
        apply_scope_review(draft, _review(draft, **{"o0.need_to_know": "uncertain"}))
    )
    assert content.offers[0].need_to_know.text == ""
    assert any(issue.kind == "uncertain_scope" for issue in content.issues)


def test_applicable_content_is_unchanged():
    draft = _draft()
    assert apply_scope_review(draft, _review(draft)) == draft


@pytest.mark.parametrize("problem", ["missing", "duplicate", "unknown", "no_reason"])
def test_incomplete_or_invalid_scope_review_fails_closed(problem):
    draft = _draft()
    data = _review(draft).model_dump()
    if problem == "missing":
        data["verdicts"].pop()
    elif problem == "duplicate":
        data["verdicts"].append(data["verdicts"][0])
    elif problem == "unknown":
        data["verdicts"][0]["value_id"] = "o99.name"
    else:
        data["verdicts"][0].update(verdict="other_programme", reason=" ")

    with pytest.raises(ScopeReviewError):
        apply_scope_review(draft, ScopeReview.model_validate(data))


def test_scope_candidates_include_item_identity_and_question_wording():
    values = scope_values(_draft())
    assert values[0].item == "YŞEY"
    assert (
        next(value for value in values if value.value_id == "q0.answer").item == "ما قيمة المساعدة؟"
    )
