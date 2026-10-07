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


def test_questions_can_be_added_and_nested(generated, services):
    client, key, night, _cash = generated
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


def test_gap_status_can_be_updated(generated, services):
    client, key, night, _cash = generated
    gap = next(
        g
        for g in services.exports.plan(services.sessions.get_active(key)).gaps
        if g.subcategory_key == night
    )

    client.post(
        f"/gaps/{gap.id}",
        data={
            "status": "resolved",
            "suggested_action": "Ask the desk",
            "suggested_contact": "",
            "back": night,
        },
    )

    updated = next(
        g for g in services.exports.plan(services.sessions.get_active(key)).gaps if g.id == gap.id
    )
    assert (updated.status, updated.suggested_action) == ("resolved", "Ask the desk")
    assert client.post("/gaps/unknown-gap", data={"status": "open"}).status_code == 404


def test_review_overview_lists_all_gaps_in_one_section(generated, services):
    client, key, _night, _cash = generated

    page = client.get("/review").data.decode()

    assert "Address gaps" in page
    assert "Not supported by sources" in page
    assert "No supporting content" in page
    gaps = page[page.index('id="gaps"') :]
    assert "Night shelter ›" in gaps
    assert "Cash assistance ›" in gaps
    assert 'name="back" value=""' in gaps

    gap = services.exports.plan(services.sessions.get_active(key)).gaps[0]
    response = client.post(f"/gaps/{gap.id}", data={"status": "resolved", "back": ""})
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
