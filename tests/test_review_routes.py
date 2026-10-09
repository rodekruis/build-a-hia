import io

import pytest
from factories import DRAFT_FOR_ROUTES, PROPOSAL, FakeModel, cited, offer, session_key_of
from openpyxl import load_workbook

from build_a_hia.services.generation import GenerationProcessor

EMPTY_DRAFT = {"offers": [], "questions": [], "issues": []}


class RoutingModel(FakeModel):
    """Answers per sub-category, so the order of generation jobs does not matter."""

    def __init__(self, by_subcategory: dict[str, dict]) -> None:
        super().__init__()
        self.by_subcategory = by_subcategory

    def parse(self, messages, schema):
        task = messages[-1]["content"]
        name = next(name for name in self.by_subcategory if f"Sub-category: {name}" in task)
        self.responses.insert(0, self.by_subcategory[name])
        return super().parse(messages, schema)


@pytest.fixture
def run_jobs(services):
    def run(*responses, by_subcategory: dict[str, dict] | None = None) -> None:
        model = RoutingModel(by_subcategory) if by_subcategory else FakeModel(*responses)
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

    return run


@pytest.fixture
def generated(ready_client, run_jobs, services):
    """Approved structure with generated content: Night shelter (1 offer) and Cash (empty)."""
    ready_client.post("/structure/propose", data={"instructions": ""})
    run_jobs(PROPOSAL)
    ready_client.post("/structure/approve", data={"version": 1})
    ready_client.post("/content/generate")
    run_jobs(by_subcategory={"Night shelter": DRAFT_FOR_ROUTES, "Cash assistance": EMPTY_DRAFT})
    key = session_key_of(ready_client)
    structure = services.structures.approved(key)[1]
    night, cash = (sub for _category, sub in structure.subcategories())
    return ready_client, key, night.key, cash.key


def _state(services, key, sub_key):
    return services.contents.state(key, sub_key)


def _offer_form(blob: str, **values: str) -> dict[str, str]:
    data = {
        "blob": blob,
        "name": "Night shelter",
        "description": "Free beds for everyone.",
        "phone_numbers": "",
        "emails": "",
        "web_urls": "",
        "address": "",
        "open_weekdays": "",
        "open_weekend": "",
        "need_to_know": "",
        "more_info": "",
        "chapter": "",
    }
    data.update(values)
    return data


def _approve_all(client, services, key, night, cash):
    client.post(
        f"/content/{cash}/empty",
        data={"blob": _state(services, key, cash).blob, "decision": "drop"},
    )
    client.post(
        f"/content/{night}/approve",
        data={"blob": _state(services, key, night).blob, "disclosure_reviewed": "y"},
    )


def test_pages_do_not_describe_generated_content_as_hidden(generated, services):
    client, key, night, cash = generated

    content_page = client.get(f"/content/{night}")
    assert content_page.status_code == 200
    assert b"visibility Hide" not in content_page.data

    _approve_all(client, services, key, night, cash)
    client.post("/download")
    download_page = client.get("/download")
    assert download_page.status_code == 200
    assert b"Latest snapshot" in download_page.data
    assert b"visibility" not in download_page.data
    assert b"Hidden rows" not in download_page.data
    assert b"hidden scaffolding row" not in download_page.data
    assert b"copy rows 2 and below" in download_page.data
    assert b"Review everything for public disclosure" in download_page.data


def test_reviewer_edits_are_flagged_and_withdraw_approval(generated, services):
    client, key, night, _cash = generated
    state = _state(services, key, night)
    client.post(f"/content/{night}/approve", data={"blob": state.blob, "disclosure_reviewed": "y"})
    assert _state(services, key, night).approved
    content = services.contents.result(key, _state(services, key, night))
    offer_id = content.offers[0].id

    response = client.post(
        f"/content/{night}/offers/{offer_id}", data=_offer_form(_state(services, key, night).blob)
    )

    assert response.status_code == 302
    state = _state(services, key, night)
    assert state.edited and not state.approved
    edited = services.contents.result(key, state).offers[0]
    assert edited.description.edited
    assert edited.description.evidence
    page = client.get(f"/content/{night}")
    assert b"Edited by reviewer" in page.data


@pytest.mark.parametrize("item_type", ["offers", "questions"])
def test_content_autosave_returns_updated_version_and_rejects_stale_edits(
    generated, services, item_type
):
    client, key, night, _cash = generated
    state = _state(services, key, night)
    content = services.contents.result(key, state)
    if item_type == "offers":
        item_id = content.offers[0].id
        data = _offer_form(state.blob, description="Updated description.")
    else:
        item_id = content.questions[0].ref
        data = {
            "blob": state.blob,
            "question": "Who can stay?",
            "answer": "Everyone.",
            "parent": "",
        }
    endpoint = f"/content/{night}/{item_type}/{item_id}"
    headers = {"Accept": "application/json"}

    response = client.post(endpoint, data=data, headers=headers)

    assert response.status_code == 200
    assert response.json == {"saved": True, "blob": _state(services, key, night).blob}
    assert response.json["blob"] != state.blob
    assert _state(services, key, night).edited
    assert not _state(services, key, night).approved
    stale = client.post(endpoint, data=data, headers=headers)
    assert stale.status_code == 409
    assert stale.json["saved"] is False
    data["blob"] = response.json["blob"]
    assert client.post(endpoint, data=data, headers=headers).status_code == 200
    data["blob"] = _state(services, key, night).blob
    data["name" if item_type == "offers" else "question"] = ""
    invalid = client.post(endpoint, data=data, headers=headers)
    assert invalid.status_code == 400
    assert invalid.json["saved"] is False


def test_stale_edits_are_rejected(generated, services):
    client, key, night, _cash = generated
    content = services.contents.result(key, _state(services, key, night))
    old_blob = _state(services, key, night).blob
    client.post(f"/content/{night}/offers/new", data=_offer_form(old_blob, name="Legal aid"))

    response = client.post(
        f"/content/{night}/offers/{content.offers[0].id}/remove",
        data={"blob": old_blob},
        follow_redirects=True,
    )

    assert b"changed in the meantime" in response.data
    assert len(services.contents.result(key, _state(services, key, night)).offers) == 2


def test_approval_needs_the_current_version(generated, services):
    client, key, night, _cash = generated
    old_blob = _state(services, key, night).blob
    client.post(f"/content/{night}/offers/new", data=_offer_form(old_blob, name="Legal aid"))

    client.post(f"/content/{night}/approve", data={"blob": old_blob})
    assert not _state(services, key, night).approved

    client.post(f"/content/{night}/approve", data={"blob": _state(services, key, night).blob})
    assert _state(services, key, night).approved


def test_content_page_puts_gaps_after_content_and_approve_next_to_regenerate(generated):
    client, _key, night, _cash = generated

    page = client.get(f"/content/{night}").data.decode()

    assert page.index('id="offers"') < page.index('id="questions"') < page.index('id="gaps"')
    assert page.index('id="gaps"') < page.index("Approve content")
    assert page.index("Approve content") < page.index("Regenerate this sub-category")
    assert "disclosure_reviewed" not in page
    assert "data-content-autosave" in page
    assert "Edit offer" not in page
    assert "Edit question" not in page
    assert "Save offer" not in page
    assert "Save question" not in page
    assert "Remove offer" in page
    assert "Remove question" in page
    assert 'name="phone_numbers"' in page
    assert 'name="answer"' in page
    assert "Follow-up to" not in page


def test_questions_can_be_added_and_nested(generated, services):
    client, key, night, _cash = generated
    page = client.get(f"/content/{night}").data.decode()
    subquestion_form = page.split('id="add-sub-question-q1"', 1)[1].split("</details>", 1)[0]
    assert "Add a sub-question" in subquestion_form
    assert 'type="hidden" name="parent" value="q1"' in subquestion_form
    assert 'name="blob"' in subquestion_form
    assert "<select" not in subquestion_form
    blob = _state(services, key, night).blob
    client.post(
        f"/content/{night}/questions/new",
        data={"blob": blob, "question": "How long?", "answer": "One week.", "parent": "q1"},
    )

    content = services.contents.result(key, _state(services, key, night))
    assert [(q.question, q.parent_ref) for q in content.questions] == [
        ("Who can stay?", ""),
        ("How long?", "q1"),
    ]
    page = client.get(f"/content/{night}").data.decode()
    assert 'class="question depth-1"' in page
    assert 'id="add-sub-question-q1"' in page
    assert f'id="add-sub-question-{content.questions[1].ref}"' not in page
    parent_card = page.split('id="question-q1"', 1)[1].split("</article>", 1)[0]
    child_card = page.split(f'id="question-{content.questions[1].ref}"', 1)[1].split(
        "</article>", 1
    )[0]
    assert ">Remove question</button>" in parent_card
    assert ">Remove sub-question</button>" in child_card
    assert 'type="hidden" name="parent" value=""' in parent_card
    assert 'type="hidden" name="parent" value="q1"' in child_card
    assert "<select" not in parent_card + child_card

    response = client.post(
        f"/content/{night}/questions/{content.questions[1].ref}",
        data={
            "blob": _state(services, key, night).blob,
            "question": "How long?",
            "answer": "Two weeks.",
            "parent": "q1",
        },
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    updated = services.contents.result(key, _state(services, key, night))
    assert updated.questions[1].parent_ref == "q1"
    assert updated.questions[1].answer.text == "Two weeks."


def test_gap_action_can_be_updated_without_status_or_contact(generated, services):
    client, key, night, _cash = generated
    gap = next(
        g
        for g in services.exports.plan(services.sessions.get_active(key)).gaps
        if g.subcategory_key == night
    )
    services.gaps.update(
        key, gap.id, status="resolved", action="Ask the desk", contact="Program coordinator"
    )

    for path in ("/review", f"/content/{night}"):
        page = client.get(path).data.decode()
        card = page.split(f'id="gap-{gap.id}"', 1)[1].split("</li>", 1)[0]
        assert "Ask the desk\nContact: Program coordinator" in card
        assert "Suggested contact" not in card
        assert 'name="suggested_contact"' not in card
        assert 'name="status"' not in card
        assert "badge" not in card
        assert "<details" not in card
        assert "data-gap-autosave" in card
        assert 'type="submit"' not in card
        assert "data-save-status" in card
        assert card.index(gap.issue) < card.index("Suggested action")
        if "Sources:" in card:
            assert card.index("Suggested action") < card.index("Sources:")

    client.post(
        f"/gaps/{gap.id}",
        data={
            "suggested_action": "Ask the program team at the desk",
            "back": night,
        },
    )

    updated = next(
        g for g in services.exports.plan(services.sessions.get_active(key)).gaps if g.id == gap.id
    )
    assert (updated.status, updated.suggested_action) == (
        "resolved",
        "Ask the program team at the desk",
    )
    assert updated.suggested_contact == ""
    assert (
        client.post("/gaps/unknown-gap", data={"suggested_action": "Ask the team"}).status_code
        == 404
    )


@pytest.mark.parametrize(
    "action, expected_status", [("Ask the program team", 200), ("", 200), ("x" * 1001, 400)]
)
def test_gap_autosave_returns_json_and_validates_input(
    generated, services, action, expected_status
):
    client, key, night, _cash = generated
    gap = next(
        gap
        for gap in services.exports.plan(services.sessions.get_active(key)).gaps
        if gap.subcategory_key == night
    )
    services.gaps.update(key, gap.id, status="open", action="Previous action", contact="")
    client.get("/review")

    response = client.post(
        f"/gaps/{gap.id}",
        data={"suggested_action": action, "back": night},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == expected_status
    assert response.json["saved"] is (expected_status == 200)
    stored = services.gaps.overrides(key)[gap.id]
    assert stored["action"] == (action if expected_status == 200 else "Previous action")
    updated = next(
        candidate
        for candidate in services.exports.plan(services.sessions.get_active(key)).gaps
        if candidate.id == gap.id
    )
    assert updated.suggested_action == stored["action"]
    with client.session_transaction() as session:
        assert not session.get("_flashes")


def test_review_overview_lists_all_gaps_in_one_section(generated, services):
    client, key, _night, _cash = generated

    page = client.get("/review").data.decode()

    assert "Review gaps" in page
    assert "Not supported by sources" in page
    assert "No supporting content" in page
    gaps = page[page.index('id="gaps"') :]
    assert "Night shelter ›" in gaps
    assert "Cash assistance ›" in gaps
    assert 'name="back" value=""' in gaps

    gap = services.exports.plan(services.sessions.get_active(key)).gaps[0]
    response = client.post(
        f"/gaps/{gap.id}", data={"suggested_action": "Ask the program team", "back": ""}
    )
    assert response.headers["Location"].endswith("/review#gaps")


def test_review_overview_lists_unreviewed_first(generated, services):
    client, key, night, _cash = generated
    page = client.get("/review").data
    assert page.index(b"Night shelter") < page.index(b"Cash assistance")
    assert b"0 of 2 reviewed" in page

    client.post(
        f"/content/{night}/approve",
        data={"blob": _state(services, key, night).blob, "disclosure_reviewed": "y"},
    )

    page = client.get("/review").data
    assert page.index(b"Cash assistance") < page.index(b"Night shelter")
    assert b"1 of 2 reviewed" in page


def test_download_is_blocked_until_everything_is_reviewed(generated):
    client, _key, _night, _cash = generated

    page = client.get("/download")
    assert b"needs review and approval" in page.data
    assert b"decide whether to keep or drop" in page.data

    response = client.post("/download", follow_redirects=True)
    assert b"Latest snapshot" not in response.data


def test_downloads_are_separate_and_clean(generated, services):
    client, key, night, cash = generated
    _approve_all(client, services, key, night, cash)

    client.post("/download")
    latest = services.exports.latest(key)
    hia = client.get(f"/download/{latest['snapshot']}/hia.xlsx")
    internal = client.get(f"/download/{latest['snapshot']}/review-internal.xlsx")

    assert hia.status_code == internal.status_code == 200
    assert hia.headers["Content-Disposition"] == 'attachment; filename="hia.xlsx"'
    assert hia.headers["Cache-Control"] == "no-store"
    workbook = load_workbook(io.BytesIO(hia.data))
    values = " ".join(
        str(c.value)
        for ws in workbook.worksheets
        for r in ws.iter_rows()
        for c in r
        if c.value is not None
    )
    assert "Night shelter" in values
    assert "Cash assistance" not in values
    assert "S1-p1-c1" not in values and "Not supported" not in values
    review = load_workbook(io.BytesIO(internal.data))
    assert review.sheetnames == ["Gaps", "Evidence", "Sources"]
    assert latest["snapshot"] in review["Gaps"]["A1"].value


def test_downloads_are_isolated_and_replaced(generated, services, app):
    client, key, night, cash = generated
    _approve_all(client, services, key, night, cash)
    client.post("/download")
    first = services.exports.latest(key)["snapshot"]

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
    assert other.get(f"/download/{first}/hia.xlsx").status_code == 302

    client.post("/download")
    assert client.get(f"/download/{first}/hia.xlsx").status_code == 302
    assert client.get("/download/zzz/hia.xlsx").status_code == 404
    assert client.get(f"/download/{first}/secrets.txt").status_code == 404


def test_regeneration_resets_review(generated, services, run_jobs):
    client, key, night, cash = generated
    _approve_all(client, services, key, night, cash)

    client.post(f"/content/{night}/regenerate")
    run_jobs({"offers": [offer(cited("Night shelter", "S1-p1-c1"))], "questions": [], "issues": []})

    assert not _state(services, key, night).approved
    assert b"needs review and approval" in client.get("/download").data
