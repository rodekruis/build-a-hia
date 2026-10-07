import pytest
from factories import PROPOSAL, FakeModel, cited, offer, question, session_key_of

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
    model = FakeModel(DRAFT)

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
    processor = make_processor(FakeModel(DRAFT, DRAFT))
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

    make_processor(EditingModel(DRAFT)).process_next()

    state = services.contents.state(key, night.key)
    assert state.status == "failed"
    assert not state.blob


def test_messages_for_deleted_sessions_are_dropped(services, make_processor, key):
    services.structures.request_proposal(_workspace(services, key))
    services.sessions.delete(key)
    model = FakeModel(PROPOSAL)

    assert make_processor(model).process_next()
    assert model.calls == []
