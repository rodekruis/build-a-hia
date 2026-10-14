import pytest
from factories import cited, offer, scope_response

from build_a_hia.services.checks import ChunkIndex, ChunkRef, check_draft
from build_a_hia.services.content import CitedText, FieldValue, SubcategoryDraft
from build_a_hia.services.scope import ScopeReview, ScopeReviewError, apply_scope_review
from build_a_hia.services.shared_contacts import apply_shared_contacts


def _index():
    return ChunkIndex(
        [
            ChunkRef("S1-p1-c1", "s1", "S1", "Services", 1, "", "RLF provides family messages."),
            ChunkRef(
                "S2-p1-c1",
                "s2",
                "S2",
                "Central contact",
                1,
                "",
                "For all RLF services contact pesquisa@example.org or 213 913 925. "
                "Visit Central Street 1, Monday to Friday 9-17.",
            ),
        ]
    )


def _data(**fields):
    return {
        "offers": [offer(cited("Family messages", "S1-p1-c1"), **fields)],
        "questions": [],
        "issues": [
            {
                "kind": "missing",
                "item": "Family messages",
                "field": "emails",
                "description": "No email in this sub-category's passages.",
                "chunk_ids": [],
            }
        ],
    }


def _review(data, field="emails", values=None):
    response = scope_response(data)
    response["shared_contacts"] = [
        {
            "offer_index": 0,
            "field": field,
            "programme": "RLF",
            "reason": "The central RLF contact supports all services, including family messages.",
            "applicability_chunk_ids": ["S1-p1-c1", "S2-p1-c1"],
            "values": values or [cited("pesquisa@example.org", "S2-p1-c1")],
        }
    ]
    return ScopeReview.model_validate(response)


def test_shared_contact_is_filled_cited_marked_and_removes_only_satisfied_gap():
    data = _data()
    draft, inherited = apply_shared_contacts(
        SubcategoryDraft.model_validate(data), _review(data), _index()
    )
    content = check_draft(
        draft,
        _index(),
        generated_on="2026-10-14",
        prompt_version="t",
        inherited_contacts=inherited,
    )
    value = content.offers[0].emails[0]
    assert value.text == "pesquisa@example.org"
    assert value.inherited_from == "RLF"
    assert {e.source_ref for e in value.evidence} == {"S1", "S2"}
    assert content.issues == []


@pytest.mark.parametrize(
    "field,values",
    [
        ("phone_numbers", [cited("213 913 925", "S2-p1-c1")]),
        ("address", [cited("Central Street 1", "S2-p1-c1")]),
        ("open_weekdays", [cited("Monday to Friday 9-17", "S2-p1-c1")]),
    ],
)
def test_shared_text_and_phone_fields_are_checked(field, values):
    data = _data()
    draft, inherited = apply_shared_contacts(
        SubcategoryDraft.model_validate(data), _review(data, field, values), _index()
    )
    result = check_draft(
        draft,
        _index(),
        generated_on="2026-10-14",
        prompt_version="t",
        inherited_contacts=inherited,
    )
    value = getattr(result.offers[0], field)
    if isinstance(value, list):
        value = value[0]
    assert value.inherited_from == "RLF"
    assert value.text == values[0]["text"]
    assert any(issue.field == "emails" for issue in result.issues)


def test_service_specific_override_is_never_replaced():
    data = _data(emails=[cited("specific@example.org", "S1-p1-c1")])
    with pytest.raises(ScopeReviewError):
        apply_shared_contacts(SubcategoryDraft.model_validate(data), _review(data), _index())


def test_scope_rejected_field_cannot_be_rescued_by_inheritance():
    data = _data(emails=[cited("wrong@example.org", "S1-p1-c1")])
    review = _review(data)
    next(v for v in review.verdicts if v.value_id == "o0.emails.0").verdict = "other_programme"
    draft = apply_scope_review(SubcategoryDraft.model_validate(data), review)
    with pytest.raises(ScopeReviewError):
        apply_shared_contacts(draft, review, _index())


@pytest.mark.parametrize(
    "problem",
    [
        "unknown_offer",
        "unknown_evidence",
        "no_reason",
        "no_programme",
        "invented_contact",
        "duplicate",
    ],
)
def test_invalid_shared_contacts_fail_closed(problem):
    data = _data()
    response = _review(data).model_dump()
    shared = response["shared_contacts"][0]
    if problem == "unknown_offer":
        shared["offer_index"] = 99
    elif problem == "unknown_evidence":
        shared["applicability_chunk_ids"] = ["S99-p1-c1"]
    elif problem == "no_reason":
        shared["reason"] = ""
    elif problem == "no_programme":
        shared["programme"] = ""
    elif problem == "invented_contact":
        shared["values"] = [cited("invented@example.org", "S2-p1-c1")]
    else:
        response["shared_contacts"].append(shared)
    with pytest.raises(ScopeReviewError):
        apply_shared_contacts(
            SubcategoryDraft.model_validate(data), ScopeReview.model_validate(response), _index()
        )


def test_legacy_stored_field_defaults_to_not_inherited():
    assert FieldValue.model_validate({"text": "Legacy value"}).inherited_from == ""


def test_no_shared_contact_proposal_preserves_missing_gap():
    data = _data()
    review = ScopeReview.model_validate(scope_response(data))
    draft, inherited = apply_shared_contacts(
        SubcategoryDraft.model_validate(data), review, _index()
    )
    result = check_draft(
        draft,
        _index(),
        generated_on="2026-10-14",
        prompt_version="t",
        inherited_contacts=inherited,
    )
    assert result.offers[0].emails == []
    assert result.issues[0].kind == "missing"


def test_rejected_shared_values_keep_missing_gap_and_no_inherited_marker():
    data = _data()
    draft = SubcategoryDraft.model_validate(data)
    # The standard checker still guards contacts if an inherited payload is invalid.
    draft.offers[0].emails = [CitedText(text="invented@example.org", chunk_ids=["S2-p1-c1"])]
    result = check_draft(
        draft,
        _index(),
        generated_on="2026-10-14",
        prompt_version="t",
        inherited_contacts={(0, "emails"): "RLF"},
    )
    assert result.offers[0].emails == []
    assert any(issue.kind == "missing" for issue in result.issues)
