import pytest
from factories import cited, offer, question, scope_response

from build_a_hia.services.checks import ChunkIndex, ChunkRef, check_draft
from build_a_hia.services.contacts import apply_contact_review
from build_a_hia.services.content import CitedText, SubcategoryDraft
from build_a_hia.services.prompts import SourceChunk, SourceMaterial
from build_a_hia.services.scope import ScopeReview, ScopeReviewError, apply_scope_review

OLD_PHONE = "213913935"
NEW_PHONE = "213 913 925"
OLD_EMAIL = "tracing@example.org"
NEW_EMAIL = "rlf@example.org"


def _sources(old_date="2013-09-27", new_date="2025-10-23"):
    return [
        SourceMaterial(
            "S1",
            "Flyer",
            "PDF",
            old_date,
            "",
            [SourceChunk("S1-p1-c1", f"Family tracing. {OLD_PHONE}, {OLD_EMAIL}.")],
        ),
        SourceMaterial(
            "S2",
            "Family tracing",
            "Web page",
            new_date,
            "https://example.org/tracing",
            [SourceChunk("S2-p1-c1", f"Family tracing. {NEW_PHONE}, {NEW_EMAIL}.")],
        ),
    ]


def _index(sources):
    return ChunkIndex(
        [
            ChunkRef(c.chunk_id, s.reference, s.reference, s.title, 1, "", c.text)
            for s in sources
            for c in s.chunks
        ]
    )


def _draft():
    return {
        "offers": [
            offer(
                cited("Family tracing", "S1-p1-c1"),
                phone_numbers=[cited(OLD_PHONE, "S1-p1-c1")],
                emails=[cited(OLD_EMAIL, "S1-p1-c1")],
            ),
            offer(
                cited("Contact in Portugal", "S2-p1-c1"),
                phone_numbers=[
                    cited(OLD_PHONE, "S1-p1-c1"),
                    cited(NEW_PHONE, "S2-p1-c1"),
                ],
            ),
        ],
        "questions": [
            question(
                "q1",
                "How can I contact family tracing?",
                cited(f"Call {OLD_PHONE} or {NEW_PHONE}.", "S1-p1-c1", "S2-p1-c1"),
            )
        ],
        "issues": [],
    }


def _review(draft):
    data = scope_response(draft)
    data["contact_conflicts"] = [
        {
            "item": "Family tracing",
            "field": field,
            "candidates": [cited(old, "S1-p1-c1"), cited(new, "S2-p1-c1")],
            "value_ids": [
                value["value_id"]
                for value in data["verdicts"]
                if value["value_id"].endswith(f".{field}.0")
                or value["value_id"].endswith(f".{field}.1")
                or (field == "phone_numbers" and value["value_id"].endswith(".answer"))
            ],
        }
        for field, old, new in [
            ("phone_numbers", OLD_PHONE, NEW_PHONE),
            ("emails", OLD_EMAIL, NEW_EMAIL),
        ]
    ]
    data["contact_conflicts"] = [
        conflict for conflict in data["contact_conflicts"] if conflict["value_ids"]
    ]
    data["contact_rewrites"] = [
        {"value_id": "q0.answer", "replacement": cited(f"Call {NEW_PHONE}.", "S2-p1-c1")}
    ]
    return ScopeReview.model_validate(data)


def test_newer_contacts_replace_old_defaults_and_both_numbers_in_public_text():
    sources = _sources()
    raw = _draft()
    draft = SubcategoryDraft.model_validate(raw)
    updated = apply_contact_review(draft, _review(raw), sources, _index(sources))
    content = check_draft(updated, _index(sources), generated_on="2026-10-14", prompt_version="t")

    assert [[p.text for p in item.phone_numbers] for item in content.offers] == [
        [NEW_PHONE],
        [NEW_PHONE],
    ]
    assert content.offers[0].emails[0].text == NEW_EMAIL
    assert content.offers[0].phone_numbers[0].evidence[0].source_ref == "S2"
    assert content.questions[0].answer.text == f"Call {NEW_PHONE}."
    gaps = [issue for issue in content.issues if issue.kind == "conflict"]
    assert gaps
    assert any(OLD_PHONE in issue.description for issue in gaps)
    assert any(OLD_EMAIL in issue.description for issue in gaps)
    assert all({e.source_ref for e in issue.evidence} == {"S1", "S2"} for issue in gaps)
    assert draft.offers[0].phone_numbers[0].text == OLD_PHONE


@pytest.mark.parametrize(
    "old_date,new_date,expected",
    [
        ("", "", NEW_PHONE),
        ("", "2025-10-23 (from page metadata)", NEW_PHONE),
        ("2026-01-01", "2025-10-23", OLD_PHONE),
    ],
)
def test_date_and_unknown_date_format_priority(old_date, new_date, expected):
    sources = _sources(old_date, new_date)
    raw = _draft()
    review = _review(raw)
    review.contact_rewrites = []
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), review, sources, _index(sources)
    )
    assert updated.offers[0].phone_numbers[0].text == expected


def test_obsolete_prose_is_withheld_when_no_safe_rewrite_is_supplied():
    sources = _sources()
    raw = _draft()
    review = _review(raw)
    review.contact_rewrites = []
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), review, sources, _index(sources)
    )
    assert updated.questions[0].answer.text == ""
    assert any(issue.field == "answer" for issue in updated.issues)


@pytest.mark.parametrize(
    "replacement",
    [
        cited(f"Call {OLD_PHONE} or {NEW_PHONE}.", "S2-p1-c1"),
        cited(f"Call {NEW_PHONE}.", "S99-p1-c1"),
        cited("Call 999999999.", "S2-p1-c1"),
        cited(f"Call {NEW_PHONE} or 999999999.", "S2-p1-c1"),
        cited(f"Call {NEW_PHONE} or invented@example.org.", "S2-p1-c1"),
        cited(f"Call {NEW_PHONE} or visit https://invented.example.org.", "S2-p1-c1"),
    ],
)
def test_invalid_or_still_conflicting_rewrites_are_withheld(replacement):
    sources = _sources()
    raw = _draft()
    review = _review(raw)
    review.contact_rewrites[0].replacement = CitedText.model_validate(replacement)
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), review, sources, _index(sources)
    )
    assert updated.questions[0].answer.text == ""


def test_same_selection_across_separate_subcategory_drafts_and_source_order():
    sources = _sources()
    for name, reverse in [("Family tracing", False), ("Contact in Portugal", True)]:
        raw = _draft()
        raw["offers"] = [
            offer(cited(name, "S1-p1-c1"), phone_numbers=[cited(OLD_PHONE, "S1-p1-c1")])
        ]
        raw["questions"] = []
        review = _review(raw)
        review.contact_rewrites = []
        if reverse:
            sources = list(reversed(sources))
            for conflict in review.contact_conflicts:
                conflict.candidates.reverse()
        updated = apply_contact_review(
            SubcategoryDraft.model_validate(raw), review, sources, _index(sources)
        )
        assert updated.offers[0].phone_numbers[0].text == NEW_PHONE


def test_no_contact_conflict_leaves_multiple_legitimate_contacts_unchanged():
    sources = _sources()
    raw = _draft()
    draft = SubcategoryDraft.model_validate(raw)
    review = ScopeReview.model_validate(scope_response(raw))
    assert apply_contact_review(draft, review, sources, _index(sources)) == draft


@pytest.mark.parametrize("date_value", ["", "2025-10-23"])
def test_equal_priority_conflicts_are_withheld_not_arbitrarily_selected(date_value):
    sources = _sources(date_value, date_value)
    sources[1] = SourceMaterial(
        "S2",
        "Another flyer",
        "PDF",
        date_value,
        "",
        sources[1].chunks,
    )
    raw = _draft()
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), _review(raw), sources, _index(sources)
    )
    assert all(not value.text for item in updated.offers for value in item.phone_numbers)
    assert updated.questions[0].answer.text == ""
    assert any("No source has clear priority" in issue.description for issue in updated.issues)


def test_url_of_downloaded_pdf_does_not_make_it_a_web_page():
    sources = _sources("", "")
    sources[0] = SourceMaterial(
        "S1",
        "Downloaded flyer",
        "PDF",
        "",
        "https://example.org/flyer.pdf",
        sources[0].chunks,
    )
    raw = _draft()
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), _review(raw), sources, _index(sources)
    )
    assert updated.offers[0].phone_numbers[0].text == NEW_PHONE


def test_contact_matching_tolerates_phone_formatting_and_international_prefix():
    sources = _sources()
    raw = _draft()
    raw["offers"][0]["phone_numbers"][0]["text"] = "+351 (213) 913-935"
    raw["questions"][0]["answer"]["text"] = "Call +351 213 913 935."
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), _review(raw), sources, _index(sources)
    )
    assert updated.offers[0].phone_numbers[0].text == NEW_PHONE
    assert updated.questions[0].answer.text == f"Call {NEW_PHONE}."


def test_question_with_superseded_contact_in_its_wording_is_withheld():
    sources = _sources()
    raw = _draft()
    raw["questions"][0]["question"] = f"Can I call {OLD_PHONE}?"
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), _review(raw), sources, _index(sources)
    )
    assert updated.questions[0].answer.text == ""


def test_explicit_targets_preserve_contact_for_a_different_role_even_if_digits_match():
    sources = _sources()
    raw = _draft()
    raw["offers"].append(
        offer(cited("Another service", "S1-p1-c1"), phone_numbers=[cited(OLD_PHONE, "S1-p1-c1")])
    )
    review = _review(raw)
    review.contact_conflicts[0].value_ids.remove("o2.phone_numbers.0")
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), review, sources, _index(sources)
    )
    assert updated.offers[2].phone_numbers[0].text == OLD_PHONE
    assert updated.offers[0].phone_numbers[0].text == NEW_PHONE


@pytest.mark.parametrize(
    "problem",
    [
        "unknown_citation",
        "not_literal",
        "unknown_target",
        "duplicate_target",
        "duplicate_rewrite",
        "mixed_sources",
        "format_only",
    ],
)
def test_invalid_contact_assessments_fail_closed(problem):
    sources = _sources()
    raw = _draft()
    data = _review(raw).model_dump()
    conflict = data["contact_conflicts"][0]
    if problem == "unknown_citation":
        conflict["candidates"][0]["chunk_ids"] = ["S99-p1-c1"]
    elif problem == "not_literal":
        conflict["candidates"][0]["text"] = "999999999"
    elif problem == "unknown_target":
        conflict["value_ids"].append("o99.phone_numbers.0")
    elif problem == "duplicate_target":
        conflict["value_ids"].append(conflict["value_ids"][0])
    elif problem == "duplicate_rewrite":
        data["contact_rewrites"].append(data["contact_rewrites"][0])
    elif problem == "mixed_sources":
        conflict["candidates"][0]["chunk_ids"].append("S2-p1-c1")
    else:
        conflict["candidates"][1] = cited("213 913 935", "S1-p1-c1")

    with pytest.raises(ScopeReviewError):
        apply_contact_review(
            SubcategoryDraft.model_validate(raw),
            ScopeReview.model_validate(data),
            sources,
            _index(sources),
        )


def test_scope_rejection_takes_priority_over_contact_correction():
    sources = _sources()
    raw = _draft()
    review = _review(raw)
    for verdict in review.verdicts:
        if verdict.value_id == "o0.phone_numbers.0":
            verdict.verdict = "other_programme"
    draft = apply_scope_review(SubcategoryDraft.model_validate(raw), review)
    updated = apply_contact_review(draft, review, sources, _index(sources))
    assert updated.offers[0].phone_numbers[0].text == ""


def test_preferred_contact_keeps_gap_with_alternative_even_when_no_replacement_is_needed():
    sources = _sources()
    raw = _draft()
    raw["offers"][0]["phone_numbers"] = [cited(NEW_PHONE, "S2-p1-c1")]
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw), _review(raw), sources, _index(sources)
    )
    assert updated.offers[0].phone_numbers[0].text == NEW_PHONE
    assert any(
        issue.item == "Family tracing" and OLD_PHONE in issue.description
        for issue in updated.issues
    )


@pytest.mark.parametrize(
    "field,old,new",
    [
        ("emails", "tracing@example.org", "newtracing@example.org"),
        ("web_urls", "https://example.org/tracing", "https://example.org/tracing-new"),
    ],
)
def test_contact_prefix_is_not_mistaken_for_the_superseded_value(field, old, new):
    sources = [
        SourceMaterial(
            "S1",
            "Flyer",
            "PDF",
            "2013-09-27",
            "",
            [
                SourceChunk("S1-p1-c1", f"Family tracing: {old}."),
            ],
        ),
        SourceMaterial(
            "S2",
            "Web page",
            "Web page",
            "2025-10-23",
            "",
            [
                SourceChunk("S2-p1-c1", f"Family tracing: {new}."),
            ],
        ),
    ]
    raw = {
        "offers": [offer(cited("Family tracing", "S1-p1-c1"), **{field: [cited(old, "S1-p1-c1")]})],
        "questions": [],
        "issues": [],
    }
    data = scope_response(raw)
    data["contact_conflicts"] = [
        {
            "item": "Family tracing",
            "field": field,
            "value_ids": [f"o0.{field}.0"],
            "candidates": [cited(old, "S1-p1-c1"), cited(new, "S2-p1-c1")],
        }
    ]
    updated = apply_contact_review(
        SubcategoryDraft.model_validate(raw),
        ScopeReview.model_validate(data),
        sources,
        _index(sources),
    )
    assert getattr(updated.offers[0], field)[0].text == new
