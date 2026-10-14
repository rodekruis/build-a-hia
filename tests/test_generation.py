from dataclasses import replace

import pytest
from factories import (
    PROPOSAL,
    FakeModel,
    add_converted_source,
    cited,
    offer,
    question,
    scope_response,
    session_key_of,
)

from build_a_hia.services.drafting import DraftingError
from build_a_hia.services.generation import GenerationProcessor
from build_a_hia.services.model import ModelError
from build_a_hia.services.structure import rename

DRAFT = {
    "offers": [
        offer(
            cited("Night shelter", "S1-p1-c1"),
            phone_numbers=[cited("+380 44 123 4567", "S1-p1-c1")],
            open_weekdays=cited("Every night", "S9-p1-c1"),
        )
    ],
    "questions": [question("q1", "Who can stay?", cited("Anyone.", "S1-p1-c1"))],
    "issues": [],
}


@pytest.fixture
def make_processor(services):
    def build(model: FakeModel) -> GenerationProcessor:
        return GenerationProcessor(
            table=services.table,
            blobs=services.blobs,
            queue=services.storage.generate_queue,
            sessions=services.sessions,
            structures=services.structures,
            contents=services.contents,
            settings=services.settings,
            model_factory=lambda: model,
        )

    return build


@pytest.fixture
def key(ready_client):
    return session_key_of(ready_client)


def _workspace(services, key):
    return services.sessions.get_active(key)


def _propose(services, make_processor, key, *responses):
    services.structures.request_proposal(_workspace(services, key))
    model = FakeModel(*responses)
    assert make_processor(model).process_next()
    return model


def _approved_structure(services, make_processor, key):
    _propose(services, make_processor, key, PROPOSAL)
    state = services.structures.state(key)
    services.structures.approve(key, state.version)
    return services.structures.approved(key)[1]


def test_structure_proposal_creates_a_draft(services, make_processor, key):
    model = _propose(services, make_processor, key, PROPOSAL)

    state, draft = services.structures.draft(key)
    assert state.version == 1
    assert state.job_status == ""
    assert state.notes == "No information about health."
    assert [c.name for c in draft.categories] == ["Shelter", "Money"]
    night, cash = (sub for _c, sub in draft.subcategories())
    assert night.chunk_ids == ["S1-p1-c1"]
    assert cash.chunk_ids == ["S1-p2-c1"]
    messages, _schema = model.calls[0]
    assert "[S1-p1-c1]" in messages[1]["content"]
    assert _workspace(services, key).model_tokens_used == 1000


def test_proposal_requires_approved_sources(services, workspace_client):
    workspace = _workspace(services, session_key_of(workspace_client))

    with pytest.raises(DraftingError):
        services.structures.request_proposal(workspace)


def test_only_one_proposal_runs_at_a_time(services, key):
    services.structures.request_proposal(_workspace(services, key))

    with pytest.raises(DraftingError):
        services.structures.request_proposal(_workspace(services, key))


def test_model_errors_mark_the_job_failed(services, make_processor, key):
    _propose(services, make_processor, key, ModelError("timeout", "The AI model took too long."))

    state = services.structures.state(key)
    assert state.job_status == "failed"
    assert state.job_error == "The AI model took too long."
    assert not state.has_draft


def test_unexpected_errors_are_not_exposed(services, make_processor, key):
    _propose(services, make_processor, key, RuntimeError("secret prompt text"))

    state = services.structures.state(key)
    assert state.job_status == "failed"
    assert "secret" not in state.job_error


def test_usage_budget_blocks_requests(services, make_processor, key):
    services.sessions.add_model_tokens(key, services.settings.max_session_model_tokens)
    model = _propose(services, make_processor, key, PROPOSAL)

    assert model.calls == []
    assert "used up its AI allowance" in services.structures.state(key).job_error


def test_source_changes_during_proposal_discard_the_result(services, make_processor, key):
    services.structures.request_proposal(_workspace(services, key))

    class ChangingModel(FakeModel):
        def parse(self, messages, schema):
            services.sessions.bump_sources_version(key)
            return super().parse(messages, schema)

    make_processor(ChangingModel(PROPOSAL)).process_next()

    state = services.structures.state(key)
    assert not state.has_draft
    assert state.job_status == "failed"


def test_revision_sends_instructions_and_current_draft(services, make_processor, key):
    _propose(services, make_processor, key, PROPOSAL)
    services.structures.request_proposal(_workspace(services, key), "Merge money into shelter.")
    model = FakeModel(PROPOSAL)
    make_processor(model).process_next()

    task = model.calls[0][0][-1]["content"]
    assert "Merge money into shelter." in task
    assert "Sub-category: Night shelter" in task
    assert services.structures.state(key).version == 2


def test_edits_require_the_current_version(services, make_processor, key):
    _propose(services, make_processor, key, PROPOSAL)
    _state, draft = services.structures.draft(key)
    sub = draft.categories[0].children[0]

    services.structures.edit(key, 1, lambda s: rename(s, sub.key, "Beds", ""))
    with pytest.raises(DraftingError):
        services.structures.edit(key, 1, lambda s: rename(s, sub.key, "Other", ""))
    assert services.structures.draft(key)[1].categories[0].children[0].name == "Beds"


def test_approval_freezes_the_version_and_edits_unfreeze_it(services, make_processor, key):
    _approved_structure(services, make_processor, key)
    state = services.structures.state(key)
    assert state.approved

    services.structures.edit(key, state.version, lambda s: None)

    assert not services.structures.state(key).approved
    assert services.structures.approved(key)[1] is not None


def test_content_is_generated_checked_and_stored(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    assert services.contents.request(_workspace(services, key), [night.key]) == 1
    model = FakeModel(DRAFT, scope_response(DRAFT))

    make_processor(model).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "done"
    assert (state.offers, state.questions, state.issues) == (1, 1, 1)
    assert state.is_current(services.structures.state(key), _workspace(services, key))
    result = services.contents.result(key, state)
    assert result.offers[0].open_weekdays.text == ""
    assert result.offers[0].phone_numbers[0].evidence[0].chunk_id == "S1-p1-c1"
    task = model.calls[0][0][-1]["content"]
    assert "Sub-category: Night shelter" in task
    assert "Approved HIA structure" in model.calls[0][0][-2]["content"]


def test_scope_rejection_is_applied_before_storing_content(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [night.key])
    review = {
        "contact_conflicts": [],
        "contact_rewrites": [],
        "verdicts": [
            {
                "value_id": value_id,
                "verdict": "other_programme",
                "reason": "This service and its facts belong to another programme.",
            }
            for value_id in ("o0.name", "o0.open_weekdays", "o0.phone_numbers.0", "q0.answer")
        ],
    }
    model = FakeModel(DRAFT, review)

    make_processor(model).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "done"
    result = services.contents.result(key, state)
    assert result.offers == []
    assert result.questions == []
    assert any(issue.kind == "wrong_programme" for issue in result.issues)
    assert len(model.calls) == 2
    assert _workspace(services, key).model_tokens_used == 3000


@pytest.mark.parametrize(
    "review, expected_error",
    [
        (
            {"verdicts": [], "contact_conflicts": [], "contact_rewrites": []},
            "programme-scope check was incomplete",
        ),
        (ModelError("timeout", "The AI model took too long."), "took too long"),
    ],
)
def test_scope_check_failure_does_not_store_unaudited_content(
    services, make_processor, key, review, expected_error
):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [night.key])

    make_processor(FakeModel(DRAFT, review)).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "failed"
    assert expected_error in state.error
    assert not state.blob


def test_scope_request_observes_tokens_consumed_by_the_drafting_request(
    services, make_processor, key
):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [night.key])

    class BudgetModel(FakeModel):
        def parse(self, messages, schema):
            result = super().parse(messages, schema)
            return replace(result, tokens=services.settings.max_session_model_tokens)

    model = BudgetModel(DRAFT, scope_response(DRAFT))
    make_processor(model).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "failed"
    assert "used up its AI allowance" in state.error
    assert not state.blob
    assert len(model.calls) == 1


def test_source_changes_during_scope_check_discard_the_result(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [night.key])

    class ChangingScopeModel(FakeModel):
        def parse(self, messages, schema):
            if len(self.calls) == 1:
                services.sessions.bump_sources_version(key)
            return super().parse(messages, schema)

    make_processor(ChangingScopeModel(DRAFT, scope_response(DRAFT))).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "failed"
    assert not state.blob


def test_empty_draft_does_not_need_a_scope_request(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [night.key])
    model = FakeModel({"offers": [], "questions": [], "issues": []})

    make_processor(model).process_next()

    assert services.contents.state(key, night.key).status == "done"
    assert len(model.calls) == 1


def test_contact_check_corrects_old_values_using_all_sources_in_the_same_two_calls(
    services, make_processor, key
):
    for text, document_date, source_format in [
        ("Family tracing phone: 213913935.", "2013-09-27", "pdf"),
        ("Family tracing phone: 213 913 925.", "2025-10-23", "html"),
    ]:
        source = add_converted_source(services, key, [text], "Family tracing")

        def metadata(source, document_date=document_date, source_format=source_format):
            source.document_date = document_date
            source.document_date_origin = "user"
            source.format = source_format
            return True

        services.sources.repository.mutate(key, source.id, metadata)
    services.sessions.approve_sources(key)
    structure = _approved_structure(services, make_processor, key)
    sub = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [sub.key])
    draft = {
        "offers": [
            offer(
                cited("Family tracing", "S2-p1-c1"),
                phone_numbers=[cited("213913935", "S2-p1-c1")],
            )
        ],
        "questions": [
            question(
                "q1",
                "How can I call?",
                cited("Call 213913935.", "S2-p1-c1"),
            )
        ],
        "issues": [],
    }
    review = scope_response(draft)
    review["contact_conflicts"] = [
        {
            "item": "Family tracing",
            "field": "phone_numbers",
            "value_ids": ["o0.phone_numbers.0", "q0.answer"],
            "candidates": [
                cited("213913935", "S2-p1-c1"),
                cited("213 913 925", "S3-p1-c1"),
            ],
        }
    ]
    review["contact_rewrites"] = [
        {
            "value_id": "q0.answer",
            "replacement": cited("Call 213 913 925.", "S3-p1-c1"),
        }
    ]
    model = FakeModel(draft, review)
    make_processor(model).process_next()

    state = services.contents.state(key, sub.key)
    assert state.status == "done"
    content = services.contents.result(key, state)
    assert content.offers[0].phone_numbers[0].text == "213 913 925"
    assert content.questions[0].answer.text == "Call 213 913 925."
    assert all(issue.kind == "conflict" for issue in content.issues)
    assert len(model.calls) == 2


def test_content_is_queued_in_structure_order(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)

    services.contents.request(_workspace(services, key))

    queue = services.storage.generate_queue
    queued = [queue.receive(visibility_seconds=60) for _ in range(2)]
    expected = [sub.key for _category, sub in structure.subcategories()]
    assert [message.payload["node"] for message in queued if message] == expected


def test_request_all_skips_current_content(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)
    workspace = _workspace(services, key)
    assert services.contents.request(workspace) == 2
    processor = make_processor(
        FakeModel(DRAFT, scope_response(DRAFT), DRAFT, scope_response(DRAFT))
    )
    processor.process_next()
    processor.process_next()

    assert services.contents.request(_workspace(services, key)) == 0

    state = services.structures.state(key)
    first = structure.categories[0].children[0]
    services.structures.edit(key, state.version, lambda s: rename(s, first.key, "Beds", ""))
    services.structures.approve(key, state.version + 1)
    assert services.contents.request(_workspace(services, key)) == 2


def test_content_generation_requires_an_approved_structure(services, make_processor, key):
    _propose(services, make_processor, key, PROPOSAL)

    with pytest.raises(DraftingError):
        services.contents.request(_workspace(services, key))


def test_structure_change_during_generation_fails_the_job(services, make_processor, key):
    structure = _approved_structure(services, make_processor, key)
    night = structure.categories[0].children[0]
    services.contents.request(_workspace(services, key), [night.key])

    class EditingModel(FakeModel):
        def parse(self, messages, schema):
            version = services.structures.state(key).version
            services.structures.edit(key, version, lambda s: None)
            services.structures.approve(key, version + 1)
            return super().parse(messages, schema)

    make_processor(EditingModel(DRAFT, scope_response(DRAFT))).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "failed"
    assert not state.blob


def test_messages_for_deleted_sessions_are_dropped(services, make_processor, key):
    services.structures.request_proposal(_workspace(services, key))
    services.sessions.delete(key)
    model = FakeModel(PROPOSAL)

    assert make_processor(model).process_next()
    assert model.calls == []
