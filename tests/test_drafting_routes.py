import re

import pytest
from factories import DRAFT_FOR_ROUTES, PROPOSAL, FakeModel, session_key_of

from build_a_hia import create_app
from build_a_hia.config import TestConfig
from build_a_hia.services.generation import GenerationProcessor


@pytest.fixture
def run_jobs(services):
    def run(*responses) -> FakeModel:
        model = FakeModel(*responses)
        processor = GenerationProcessor(
            table=services.table,
            blobs=services.blobs,
            queue=services.storage.generate_queue,
            sessions=services.sessions,
            structures=services.structures,
            contents=services.contents,
            settings=services.settings,
            model_factory=lambda: model,
        )
        while processor.process_next():
            pass
        return model

    return run


@pytest.fixture
def drafted(ready_client, run_jobs):
    ready_client.post("/structure/propose", data={"instructions": ""})
    run_jobs(PROPOSAL)
    return ready_client


def _draft(services, client):
    return services.structures.draft(session_key_of(client))


def test_structure_page_asks_for_approved_sources(workspace_client):
    response = workspace_client.get("/structure")

    assert response.status_code == 200
    assert b"Approve the sources" in response.data


def test_structure_page_reports_missing_model_configuration(storage_dir):
    class Config(TestConfig):
        HIA_LOCAL_STORAGE_DIR = str(storage_dir)

    client = create_app(Config).test_client()
    client.post(
        "/context",
        data={
            "country": "X",
            "situation": "Y",
            "target_group": "Z",
            "locations": "W",
            "output_language": "en",
        },
    )
    response = client.post("/structure/propose", data={"instructions": ""}, follow_redirects=True)

    assert b"not configured" in response.data


def test_proposal_is_queued_and_polled(ready_client):
    response = ready_client.post("/structure/propose", data={"instructions": "Keep it short"})

    assert response.status_code == 302
    status = ready_client.get("/structure/status")
    assert b'data-poll-active="true"' in status.data
    assert b'data-poll-reload="true"' in status.data


def test_draft_is_shown_with_model_notes(drafted):
    response = drafted.get("/structure")

    assert b"Night shelter" in response.data
    assert b"No information about health." in response.data
    assert b"Approve structure" in response.data


def test_rename_add_move_and_remove(drafted, services):
    state, draft = _draft(services, drafted)
    shelter, money = draft.categories
    night = shelter.children[0]

    drafted.post(
        "/structure/rename",
        data={"version": 1, "key": night.key, "name": "Beds", "description": "Free beds."},
    )
    drafted.post("/structure/add", data={"version": 2, "parent": shelter.key, "name": "Rent"})
    drafted.post("/structure/up", data={"version": 3, "key": money.key})
    drafted.post("/structure/move-to", data={"version": 4, "key": night.key, "target": money.key})
    drafted.post("/structure/remove", data={"version": 5, "key": shelter.key})

    state, draft = _draft(services, drafted)
    assert state.version == 6
    assert [(c.name, [s.name for s in c.children]) for c in draft.categories] == [
        ("Money", ["Cash assistance", "Beds"])
    ]


def test_stale_edits_are_rejected(drafted, services):
    _state, draft = _draft(services, drafted)
    key = draft.categories[0].key

    drafted.post("/structure/rename", data={"version": 1, "key": key, "name": "A"})
    response = drafted.post(
        "/structure/rename", data={"version": 1, "key": key, "name": "B"}, follow_redirects=True
    )

    assert b"changed in the meantime" in response.data
    assert _draft(services, drafted)[1].categories[0].name == "A"


def test_invalid_keys_are_rejected(drafted, services):
    drafted.post("/structure/remove", data={"version": 1, "key": "../../x"})

    assert _draft(services, drafted)[0].version == 1


def test_merge_shows_preview_before_applying(drafted, services):
    _state, draft = _draft(services, drafted)
    shelter, money = draft.categories

    preview = drafted.post(
        "/structure/merge", data={"version": 1, "key": money.key, "target": shelter.key}
    )
    assert preview.status_code == 200
    assert b"Cash assistance" in preview.data
    assert _draft(services, drafted)[0].version == 1

    drafted.post(
        "/structure/merge",
        data={"version": 1, "key": money.key, "target": shelter.key, "confirm": "y"},
    )
    state, draft = _draft(services, drafted)
    assert [(c.name, [s.name for s in c.children]) for c in draft.categories] == [
        ("Shelter", ["Night shelter", "Cash assistance"])
    ]


def test_duplicate_names_block_approval(drafted, services):
    _state, draft = _draft(services, drafted)
    shelter, money = draft.categories
    drafted.post("/structure/rename", data={"version": 1, "key": money.key, "name": "shelter"})

    page = drafted.get("/structure")
    assert b"is used more than once" in page.data
    drafted.post("/structure/approve", data={"version": 2})
    assert not services.structures.state(session_key_of(drafted)).approved


def test_approval_leads_to_content_generation(drafted, services, run_jobs):
    response = drafted.post("/structure/approve", data={"version": 1})
    assert response.headers["Location"].endswith("/content")

    page = drafted.get("/content")
    assert b"Generate content for 2 sub-categories" in page.data
    assert b"Continue to review" not in page.data

    drafted.post("/content/generate")
    status = drafted.get("/content/status")
    assert b'data-poll-active="true"' in status.data
    assert b"Continue to review" not in status.data
    run_jobs(DRAFT_FOR_ROUTES, DRAFT_FOR_ROUTES)

    page = drafted.get("/content")
    assert b"All sub-categories have current content" in page.data
    assert b"1 offer," in page.data
    assert b'href="/review">Continue to review' in page.data


def test_content_page_shows_evidence_and_escapes_text(drafted, services, run_jobs):
    drafted.post("/structure/approve", data={"version": 1})
    drafted.post("/content/generate")
    run_jobs(DRAFT_FOR_ROUTES, DRAFT_FOR_ROUTES)
    _state, structure = services.structures.approved(session_key_of(drafted))
    night = structure.categories[0].children[0]

    response = drafted.get(f"/content/{night.key}")

    assert response.status_code == 200
    assert b"Offer #2" in response.data
    assert b"Night shelter" in response.data
    assert b"S1" in response.data and b"page 1" in response.data
    assert b"**Free** &amp; open" in response.data
    assert b"<script>" not in response.data
    assert re.search(rb"Not supported by sources", response.data)


def test_content_detail_validates_keys(drafted):
    assert drafted.get("/content/zzz").status_code == 404
    assert drafted.get("/content/deadbeef").status_code == 404


def test_structure_is_isolated_between_sessions(drafted, app, services):
    other = app.test_client()
    other.post(
        "/context",
        data={
            "country": "X",
            "situation": "Y",
            "target_group": "Z",
            "locations": "W",
            "output_language": "en",
        },
    )
    _state, draft = _draft(services, drafted)

    other.post("/structure/remove", data={"version": 1, "key": draft.categories[0].key})

    assert len(_draft(services, drafted)[1].categories) == 2
    assert b"Night shelter" not in other.get("/structure").data
