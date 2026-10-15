import pytest
from factories import cited, offer, question

from build_a_hia.services.checks import (
    ChunkIndex,
    ChunkRef,
    check_draft,
    clean_markdown,
    normalize_url,
    phone_supported,
)
from build_a_hia.services.content import SubcategoryDraft

TEXT = (
    "Night shelter at Central Station 5, Kyiv. Call +380 (44) 123-45-67 or 1545. "
    "Email Shelter@Example.org. Website: www.example.org/shelter/"
)


def _index() -> ChunkIndex:
    return ChunkIndex(
        [
            ChunkRef("S1-p1-c1", "src1", "S1", "Guide", 1, "", TEXT),
            ChunkRef(
                "S1-p2-c1", "src1", "S1", "Guide", 2, "", "Families can apply at the town hall."
            ),
        ]
    )


def _check(**draft):
    data = {"offers": [], "questions": [], "issues": [], **draft}
    return check_draft(
        SubcategoryDraft.model_validate(data),
        _index(),
        generated_on="2026-10-07",
        prompt_version="t",
    )


def test_supported_offer_keeps_values_and_evidence():
    content = _check(
        offers=[
            offer(
                cited("Night shelter", "S1-p1-c1"),
                address=cited("Central Station 5, Kyiv", "[S1-p1-c1]"),
                phone_numbers=[cited("+380 44 123 45 67", "S1-p1-c1"), cited("1545", "S1-p1-c1")],
                emails=[cited("mailto:shelter@example.org", "S1-p1-c1")],
                web_urls=[cited("https://www.example.org/shelter", "S1-p1-c1")],
            )
        ]
    )

    [result] = content.offers
    assert result.address.text == "Central Station 5, Kyiv"
    assert result.address.evidence[0].source_ref == "S1"
    assert result.address.evidence[0].page == 1
    assert [value.text for value in result.phone_numbers] == ["+380 44 123 45 67", "1545"]
    assert [value.text for value in result.emails] == ["shelter@example.org"]
    assert [value.text for value in result.web_urls] == ["https://www.example.org/shelter"]
    assert content.issues == []


def test_fields_citing_unknown_passages_are_blanked():
    content = _check(
        offers=[
            offer(
                cited("Night shelter", "S1-p1-c1"),
                description=cited("Open to all.", "S9-p1-c1"),
                need_to_know=cited("Bring ID."),
            )
        ]
    )

    [result] = content.offers
    assert result.description.text == ""
    assert result.need_to_know.text == ""
    assert [issue.kind for issue in content.issues] == ["unsupported", "unsupported"]


def test_contact_values_must_appear_in_cited_passages():
    content = _check(
        offers=[
            offer(
                cited("Night shelter", "S1-p1-c1"),
                phone_numbers=[cited("+380 44 999 99 99", "S1-p1-c1")],
                emails=[cited("help@example.org", "S1-p1-c1")],
                web_urls=[
                    cited("https://example.org/other", "S1-p1-c1"),
                    cited("javascript:alert(1)", "S1-p1-c1"),
                ],
            )
        ]
    )

    [result] = content.offers
    assert result.phone_numbers == []
    assert result.emails == []
    assert result.web_urls == []
    assert [issue.kind for issue in content.issues] == ["unverified"] * 3


def test_offer_without_supported_name_is_dropped():
    content = _check(offers=[offer(cited("Invented service"))])

    assert content.offers == []
    assert content.issues[0].item == "Invented service"


def test_public_text_is_sanitized():
    content = _check(
        offers=[
            offer(
                cited("Night <b>shelter</b> [S1-p1-c1]", "S1-p1-c1"),
                more_info=cited(
                    "# Title\n<script>x</script>Details (S1-p1-c1)\n![img](https://x/y.png)"
                    "[click](javascript:evil)",
                    "S1-p1-c1",
                ),
            )
        ]
    )

    [result] = content.offers
    assert result.name.text == "Night shelter"
    assert result.more_info.text == "### Title\nxDetails\nclick"


def test_questions_are_single_line_and_nested_safely():
    answer = cited("Apply at the town hall.", "S1-p2-c1")
    content = _check(
        questions=[
            question("q1", "## Who can\n apply?", answer),
            question("q2", "Where?", answer, parent_ref="q1"),
            question("q3", "Loop A", answer, parent_ref="q4"),
            question("q4", "Loop B", answer, parent_ref="q3"),
            question("q5", "Orphan", answer, parent_ref="missing"),
            question("q6", "Unsupported", cited("Maybe.")),
            question("q7", "Child of unsupported", answer, parent_ref="q6"),
            question("q8", "Child of orphan", answer, parent_ref="q7"),
            question("q9", "Grandchild of orphan", answer, parent_ref="q8"),
        ]
    )

    by_ref = {item.ref: item for item in content.questions}
    assert by_ref["q1"].question == "Who can apply?"
    assert by_ref["q2"].parent_ref == "q1"
    assert by_ref["q5"].parent_ref == ""
    assert "" in (by_ref["q3"].parent_ref, by_ref["q4"].parent_ref)
    assert "q6" not in by_ref
    assert by_ref["q7"].parent_ref == ""
    assert by_ref["q8"].parent_ref == "q7"
    assert by_ref["q9"].parent_ref == "q7"
    assert all(
        not by_ref[item.parent_ref].parent_ref for item in content.questions if item.parent_ref
    )


def test_duplicate_question_refs_become_unique():
    answer = cited("Apply at the town hall.", "S1-p2-c1")
    content = _check(
        questions=[
            question("a", "One?", answer),
            question("a-3", "Two?", answer),
            question("a", "Three?", answer),
        ]
    )

    refs = [item.ref for item in content.questions]
    assert len(set(refs)) == 3
    assert all(len(ref) <= 40 for ref in refs)


@pytest.mark.parametrize("order", [("q1", "q2", "q3", "q4"), ("q4", "q3", "q2", "q1")])
def test_question_chains_are_flattened_to_one_level(order):
    questions = {
        ref: question(
            ref,
            f"Question {ref}?",
            cited(f"Answer {ref}.", "S1-p2-c1"),
            parent_ref=parent,
        )
        for ref, parent in [("q1", ""), ("q2", "q1"), ("q3", "q2"), ("q4", "q3")]
    }

    content = _check(questions=[questions[ref] for ref in order])

    assert [item.ref for item in content.questions] == list(order)
    assert {item.ref: item.parent_ref for item in content.questions} == {
        "q1": "",
        "q2": "q1",
        "q3": "q1",
        "q4": "q1",
    }
    assert all(item.question == f"Question {item.ref}?" for item in content.questions)
    assert all(item.answer.text == f"Answer {item.ref}." for item in content.questions)
    assert all(item.answer.evidence[0].chunk_id == "S1-p2-c1" for item in content.questions)
    assert {issue.item_ref for issue in content.issues if issue.kind == "structure"} == {
        "q3",
        "q4",
    }


def test_model_issues_are_kept_with_evidence():
    content = _check(
        issues=[
            {
                "kind": "conflict",
                "item": "Night shelter",
                "field": "open_weekdays",
                "description": "Two different opening times.",
                "chunk_ids": ["S1-p1-c1", "S7-p1-c1"],
            }
        ]
    )

    [issue] = content.issues
    assert issue.kind == "conflict"
    assert [item.chunk_id for item in issue.evidence] == ["S1-p1-c1"]


def test_phone_matching_tolerates_formatting_but_not_partial_numbers():
    assert phone_supported("0044 123 4567", "Tel. +380 (44) 123-45-67")
    assert not phone_supported("123", "Call 1234")
    assert phone_supported("1545", "Hotline 1545 (free)")


def test_url_normalization():
    assert normalize_url("example.org/help") == "https://example.org/help"
    assert normalize_url("ftp://example.org") == ""
    assert normalize_url("not a url") == ""


def test_clean_markdown_demotes_top_headings_only():
    assert clean_markdown("# A\n## B\n### C\n#### D") == "### A\n### B\n### C\n#### D"
