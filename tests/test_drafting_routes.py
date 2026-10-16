import re

import pytest
from bs4 import BeautifulSoup
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


def test_structure_edit_actions_are_grouped_by_operation_and_keep_their_forms(drafted):
    page = BeautifulSoup(drafted.get("/structure").data, "html.parser")
    edits = [
        edit
        for edit in page.select("details.tree-edit")
        if (summary := edit.find("summary")) is not None
        and summary.get_text(strip=True) in {"Edit category", "Edit sub-category"}
    ]
    assert edits
    ids = [element["id"] for element in page.select("[id]")]
    assert len(ids) == len(set(ids))
    for edit in edits:
        summary = edit.find("summary")
        assert summary is not None
        actions = edit.find("div", class_="source-actions", recursive=False)
        assert actions is not None
        action_labels = [button.get_text(strip=True) for button in actions.find_all("button")]
        assert action_labels[0] == "Save"
        assert all(
            label in {"Save", "Move up", "Move down", "Remove", "Remove with its sub-categories"}
            for label in action_labels
        )
        target_forms = edit.select("form.tree-target-form")
        expected_labels = (
            ["Move", "Merge"] if summary.get_text(strip=True) == "Edit sub-category" else ["Merge"]
        )
        assert [
            button.get_text(strip=True) for button in edit.select("form.tree-target-form > button")
        ] == expected_labels
        assert [
            label.get_text(strip=True) for label in edit.select("form.tree-target-form > label")
        ] == (["Move to category", "Merge into"] if len(expected_labels) == 2 else ["Merge into"])
        assert list(actions.find_next_siblings("form")) == target_forms
        for form in target_forms:
            assert form.find("select", attrs={"name": "target"}) is not None
            assert len(form.find_all("button")) == 1
        assert not edit.select("form form")
        for button in edit.find_all("button"):
            form = (
                edit.find("form", id=button["form"])
                if button.has_attr("form")
                else button.find_parent("form")
            )
            assert form is not None
            assert form["method"] == "post"
            for field in ("csrf_token", "version", "key"):
                assert form.find("input", attrs={"name": field}) is not None
            if button.get_text(strip=True) == "Save":
                assert form["action"] == "/structure/rename"
                assert form.find("input", attrs={"name": "name"}) is not None
                assert form.find("textarea", attrs={"name": "description"}) is not None


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


@pytest.mark.parametrize("level", ["category", "subcategory"])
def test_merge_applies_immediately_and_rejects_repeated_submission(drafted, services, level):
    _state, draft = _draft(services, drafted)
    shelter, money = draft.categories
    source = money if level == "category" else money.children[0]
    target = shelter if level == "category" else shelter.children[0]
    data = {"version": 1, "key": source.key, "target": target.key}

    response = drafted.post("/structure/merge", data=data)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/structure")
    state, draft = _draft(services, drafted)
    assert state.version == 2
    assert draft.find(target.key)[1].name == target.name
    assert [(c.name, [s.name for s in c.children]) for c in draft.categories] == (
        [("Shelter", ["Night shelter", "Cash assistance"])]
        if level == "category"
        else [("Shelter", ["Night shelter"]), ("Money", [])]
    )

    repeated = drafted.post("/structure/merge", data=data, follow_redirects=True)
    assert b"changed in the meantime" in repeated.data
    repeated_state, repeated_draft = _draft(services, drafted)
    assert repeated_state.version == 2
    assert repeated_draft == draft


@pytest.mark.parametrize("case", ["self", "mixed_levels", "unknown_target", "missing_target"])
def test_invalid_direct_merges_leave_the_draft_unchanged(drafted, services, case):
    _state, draft = _draft(services, drafted)
    shelter, money = draft.categories
    target = {
        "self": shelter.key,
        "mixed_levels": money.children[0].key,
        "unknown_target": "00000000",
        "missing_target": "",
    }[case]

    response = drafted.post(
        "/structure/merge",
        data={"version": 1, "key": shelter.key, "target": target},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b'class="flash flash-error"' in response.data
    state, unchanged = _draft(services, drafted)
    assert state.version == 1
    assert unchanged == draft


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
    assert b"Continue to download" not in page.data

    drafted.post("/content/generate")
    status = drafted.get("/content/status")
    assert b'data-poll-active="true"' in status.data
    assert b"Continue to download" not in status.data
    run_jobs(DRAFT_FOR_ROUTES, DRAFT_FOR_ROUTES)

    page = drafted.get("/content")
    assert b"All sub-categories have current content" in page.data
    assert b"1 offer," in page.data
    assert b"Needs review" in page.data
    assert b'href="/download">Continue to download' in page.data


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
