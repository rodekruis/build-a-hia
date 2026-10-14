import io

import pytest
from factories import (
    DRAFT_FOR_ROUTES,
    PROPOSAL,
    FakeModel,
    add_converted_source,
    cited,
    offer,
    question,
    scope_response,
    session_key_of,
    with_scope_reviews,
)
from openpyxl import load_workbook

from build_a_hia.services.generation import GenerationProcessor
from build_a_hia.services.scope import ScopeReview

EMPTY_DRAFT = {"offers": [], "questions": [], "issues": []}


class RoutingModel(FakeModel):
    """Answers per sub-category, so the order of generation jobs does not matter."""

    def __init__(self, by_subcategory: dict[str, dict]) -> None:
        super().__init__()
        self.by_subcategory = by_subcategory

    def parse(self, messages, schema):
        if schema is ScopeReview:
            self.responses.insert(0, scope_response(self.last_draft))
            return super().parse(messages, schema)
        task = messages[-1]["content"]
        name = next(name for name in self.by_subcategory if f"Sub-category: {name}" in task)
        self.last_draft = self.by_subcategory[name]
        self.responses.insert(0, self.last_draft)
        return super().parse(messages, schema)


@pytest.fixture
def run_jobs(services):
    def run(*responses, by_subcategory: dict[str, dict] | None = None) -> None:
        model = (
            RoutingModel(by_subcategory)
            if by_subcategory
            else FakeModel(*with_scope_reviews(responses))
        )
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


def test_conversion_warning_reaches_review_and_internal_workbook(generated, services):
    client, key, night, cash = generated
    [source] = services.sources.list_for(key)
    limitation = "Very little text could be read from page(s) 2."

    def warn(source):
        source.limitations = [limitation]
        return True

    services.sources.repository.mutate(key, source.id, warn)
    page = client.get("/review").data
    assert b"Conversion problem" in page and limitation.encode() in page
    plan = services.exports.plan(services.sessions.get_active(key))
    gap = next(gap for gap in plan.gaps if gap.kind == "conversion")
    assert gap.source_reference == source.reference
    _approve_all(client, services, key, night, cash)
    client.post("/download")
    latest = services.exports.latest(key)
    response = client.get(f"/download/{latest['snapshot']}/review-internal.xlsx")
    workbook = load_workbook(io.BytesIO(response.data))
    values = [str(cell.value) for row in workbook["Gaps"] for cell in row]
    assert any(limitation in value for value in values)
    assert "Conversion problem" in values


def test_scope_rejections_stay_out_of_approved_public_export_but_keep_cited_gaps(
    generated, services, run_jobs
):
    client, key, night, cash = generated
    draft = {
        "offers": [
            offer(
                cited("Night shelter", "S1-p1-c1"),
                need_to_know=cited("250 TL monthly per person.", "S1-p1-c1"),
            ),
            offer(cited("T-SUY Projesi", "S1-p1-c1")),
        ],
        "questions": [question("q1", "ما قيمة المساعدة؟", cited("250 ليرة شهرياً.", "S1-p1-c1"))],
        "issues": [],
    }
    review = scope_response(
        draft,
        **{
            "o0.need_to_know": "other_programme",
            "o1.name": "other_programme",
            "q0.answer": "other_programme",
        },
    )
    client.post(f"/content/{night}/regenerate")
    run_jobs(draft, review)

    assert b"Information for another programme" in client.get("/review").data
    _approve_all(client, services, key, night, cash)
    assert _state(services, key, night).approved
    client.post("/download")
    latest = services.exports.latest(key)

    def values(filename):
        response = client.get(f"/download/{latest['snapshot']}/{filename}")
        assert response.status_code == 200
        workbook = load_workbook(io.BytesIO(response.data))
        return "\n".join(
            str(cell.value)
            for sheet in workbook.worksheets
            for row in sheet
            for cell in row
            if cell.value is not None
        )

    public = values("hia.xlsx")
    assert "Night shelter" in public
    assert "250" not in public
    assert "T-SUY" not in public
    assert "ما قيمة المساعدة" not in public
    internal = values("review-internal.xlsx")
    assert "Information for another programme" in internal
    assert "S1-p1-c1" in internal
    assert "T-SUY Projesi" in internal
    assert "Find evidence for the intended programme" in internal


def test_contact_precedence_is_consistent_in_approved_subcategories_and_exports(
    ready_client, services, run_jobs
):
    client = ready_client
    key = session_key_of(client)
    for phone, email, document_date, source_format in [
        ("213913935", "tracing@example.org", "2013-09-27", "pdf"),
        ("213 913 925", "rlf@example.org", "2025-10-23", "html"),
    ]:
        source = add_converted_source(
            services,
            key,
            [f"Family tracing: {phone}, {email}."],
            "Family tracing",
        )

        def metadata(source, document_date=document_date, source_format=source_format):
            source.document_date = document_date
            source.document_date_origin = "user"
            source.format = source_format
            return True

        services.sources.repository.mutate(key, source.id, metadata)
    services.sessions.approve_sources(key)
    proposal = {
        "categories": [
            {
                "name": "Family tracing",
                "description": "",
                "subcategories": [
                    {"name": name, "description": "", "chunk_ids": ["S2-p1-c1"]}
                    for name in ("Pesquisa de familiares", "Contacto em Portugal")
                ],
            }
        ],
        "notes": "",
    }
    client.post("/structure/propose", data={"instructions": ""})
    run_jobs(proposal)
    client.post("/structure/approve", data={"version": 1})
    client.post("/content/generate")
    scripts = []
    for name in ("Pesquisa de familiares", "Contacto em Portugal"):
        draft = {
            "offers": [
                offer(
                    cited(name, "S2-p1-c1"),
                    phone_numbers=[cited("213913935", "S2-p1-c1")],
                    emails=[cited("tracing@example.org", "S2-p1-c1")],
                )
            ],
            "questions": [
                question(
                    "q1",
                    f"How can I contact {name}?",
                    cited("Call 213913935 or 213 913 925.", "S2-p1-c1", "S3-p1-c1"),
                )
            ],
            "issues": [],
        }
        review = scope_response(draft)
        review["contact_conflicts"] = [
            {
                "item": "Family tracing",
                "field": field,
                "value_ids": targets,
                "candidates": [cited(old, "S2-p1-c1"), cited(new, "S3-p1-c1")],
            }
            for field, targets, old, new in [
                ("phone_numbers", ["o0.phone_numbers.0", "q0.answer"], "213913935", "213 913 925"),
                ("emails", ["o0.emails.0"], "tracing@example.org", "rlf@example.org"),
            ]
        ]
        review["contact_rewrites"] = [
            {
                "value_id": "q0.answer",
                "replacement": cited("Call 213 913 925.", "S3-p1-c1"),
            }
        ]
        scripts.extend([draft, review])
    run_jobs(*scripts)
    structure = services.structures.approved(key)[1]
    for _category, sub in structure.subcategories():
        state = _state(services, key, sub.key)
        assert state.status == "done"
        client.post(
            f"/content/{sub.key}/approve",
            data={"blob": state.blob, "disclosure_reviewed": "y"},
        )
        assert _state(services, key, sub.key).approved
    client.post("/download")
    latest = services.exports.latest(key)

    def values(filename):
        response = client.get(f"/download/{latest['snapshot']}/{filename}")
        assert response.status_code == 200
        workbook = load_workbook(io.BytesIO(response.data))
        return [
            str(cell.value)
            for sheet in workbook.worksheets
            for row in sheet
            for cell in row
            if cell.value is not None
        ]

    public = values("hia.xlsx")
    assert public.count("213 913 925") == 2
    assert public.count("rlf@example.org") == 2
    assert public.count("Call 213 913 925.") == 2
    assert "213913935" not in "\n".join(public)
    assert "tracing@example.org" not in "\n".join(public)
    internal = "\n".join(values("review-internal.xlsx"))
    assert "213913935" in internal and "tracing@example.org" in internal
    assert "Conflicting sources" in internal
    assert "S2-p1-c1" in internal and "S3-p1-c1" in internal


def test_shared_contacts_reach_separate_offers_review_and_exports_with_inherited_markers(
    ready_client, services, run_jobs
):
    client = ready_client
    key = session_key_of(client)
    add_converted_source(
        services,
        key,
        ["RLF offers Family messages and Trace the Face. Both services use the central RLF team."],
        "RLF services",
    )
    add_converted_source(
        services,
        key,
        [
            "RLF central team handles all RLF services, including family messages "
            "and Trace the Face. "
            "Call 213 913 925 or pesquisa@example.org. The central team is at Central Street 1."
        ],
        "Shared RLF contact",
    )
    services.sessions.approve_sources(key)
    client.post("/structure/propose", data={"instructions": ""})
    run_jobs(PROPOSAL)
    client.post("/structure/approve", data={"version": 1})
    client.post("/content/generate")
    scripts = []
    for name in ("Family messages", "Trace the Face"):
        draft = {
            "offers": [offer(cited(name, "S2-p1-c1"))],
            "questions": [],
            "issues": [
                {
                    "kind": "missing",
                    "item": name,
                    "field": field,
                    "description": "The linked passages omit this contact.",
                    "chunk_ids": [],
                }
                for field in ("phone_numbers", "emails", "address", "open_weekdays")
            ],
        }
        review = scope_response(draft)
        review["shared_contacts"] = [
            {
                "offer_index": 0,
                "field": field,
                "programme": "RLF",
                "reason": "The central RLF team explicitly handles this service.",
                "applicability_chunk_ids": ["S2-p1-c1", "S3-p1-c1"],
                "values": [cited(text, "S3-p1-c1")],
            }
            for field, text in [
                ("phone_numbers", "213 913 925"),
                ("emails", "pesquisa@example.org"),
                ("address", "Central Street 1"),
            ]
        ]
        scripts.extend([draft, review])
    run_jobs(*scripts)
    structure = services.structures.approved(key)[1]
    for _category, sub in structure.subcategories():
        state = _state(services, key, sub.key)
        assert state.status == "done"
        content = services.contents.result(key, state)
        item = content.offers[0]
        assert item.emails[0].inherited_from == "RLF"
        assert item.phone_numbers[0].inherited_from == "RLF"
        assert {e.chunk_id for e in item.phone_numbers[0].evidence} == {
            "S2-p1-c1",
            "S3-p1-c1",
        }
        assert item.address.inherited_from == "RLF"
        assert item.open_weekdays.text == ""
        assert [issue.field for issue in content.issues] == ["open_weekdays"]
        page = client.get(f"/content/{sub.key}").data
        assert b"Inherited programme contact: RLF" in page
        client.post(
            f"/content/{sub.key}/approve",
            data={"blob": state.blob, "disclosure_reviewed": "y"},
        )
        assert _state(services, key, sub.key).approved
    client.post("/download")
    latest = services.exports.latest(key)

    def values(filename):
        response = client.get(f"/download/{latest['snapshot']}/{filename}")
        assert response.status_code == 200
        workbook = load_workbook(io.BytesIO(response.data))
        return "\n".join(
            str(cell.value)
            for sheet in workbook.worksheets
            for row in sheet
            for cell in row
            if cell.value is not None
        )

    public = values("hia.xlsx")
    assert public.count("pesquisa@example.org") == 2
    assert public.count("213 913 925") == 2
    assert "Inherited programme contact" not in public
    internal = values("review-internal.xlsx")
    assert "Inherited programme contact: RLF" in internal
    assert "S2-p1-c1" in internal
    assert "S3-p1-c1" in internal


def test_gap_noise_is_reduced_from_26_issues_to_four_gaps_in_review_and_export(
    ready_client, services, run_jobs
):
    client = ready_client
    key = session_key_of(client)
    recent = services.sources.repository.list_for(key)[0]

    def set_date(source):
        source.document_date = "2025-10-02"
        source.document_date_origin = "user"
        return True

    services.sources.repository.mutate(key, recent.id, set_date)
    undated = add_converted_source(services, key, ["Service contact."], "Undated page")
    old = add_converted_source(services, key, ["Old service guidance."], "Old guide")

    def set_old_date(source):
        source.document_date = "2024-07-01"
        source.document_date_origin = "user"
        return True

    services.sources.repository.mutate(key, old.id, set_old_date)
    services.sessions.approve_sources(key)
    client.post("/structure/propose", data={"instructions": ""})
    run_jobs(PROPOSAL)
    client.post("/structure/approve", data={"version": 1})
    client.post("/content/generate")
    drafts = []
    for name in ("Night shelter", "Cash assistance"):
        issues = (
            [
                {
                    "kind": "missing",
                    "item": name,
                    "field": field,
                    "description": "Optional field not given.",
                    "chunk_ids": [],
                }
                for field in ("address", "open_weekdays", "open_weekend")
            ]
            + [
                {
                    "kind": "outdated",
                    "item": name,
                    "field": field,
                    "description": "Possibly outdated.",
                    "chunk_ids": [chunk],
                }
                for field in ("description", "phone_numbers", "address")
                for chunk in ("S1-p1-c1", "S2-p1-c1", "S3-p1-c1")
            ]
            + [
                {
                    "kind": "conflict",
                    "item": name,
                    "field": "need_to_know",
                    "description": "Eligibility differs between sources.",
                    "chunk_ids": ["S1-p1-c1"],
                }
            ]
        )
        drafts.append(
            {"offers": [offer(cited(name, "S1-p1-c1"))], "questions": [], "issues": issues}
        )
    run_jobs(*drafts)
    workspace = services.sessions.get_active(key)
    plan = services.exports.plan(workspace)
    assert sum(len(sub.content.issues) for sub in plan.subcategories) == 26
    assert len(plan.gaps) == 4
    assert sorted(gap.kind for gap in plan.gaps) == ["conflict", "conflict", "outdated", "undated"]
    overview = client.get("/review").data.decode()
    assert "Conflicting sources: 2" in overview
    assert "Possibly outdated: 1" in overview
    assert "Source date unknown: 1" in overview
    assert "Optional field not given" not in overview
    assert f"src-{recent.id}-date" not in overview
    date_gap = f"src-{undated.id}-date"
    client.post(
        f"/gaps/{date_gap}",
        data={
            "status": "resolved",
            "suggested_action": "Checked with service",
            "suggested_contact": "Service desk",
            "back": "",
        },
    )
    for sub in plan.subcategories:
        page = client.get(f"/content/{sub.key}").data.decode()
        assert "Eligibility differs" in page
        assert "Possibly outdated." not in page
        assert "Optional field not given" not in page
        client.post(
            f"/content/{sub.key}/approve",
            data={
                "blob": sub.state.blob,
                "disclosure_reviewed": "y",
            },
        )
    client.post("/download")
    snapshot = services.exports.latest(key)["snapshot"]
    response = client.get(f"/download/{snapshot}/review-internal.xlsx")
    workbook = load_workbook(io.BytesIO(response.data))
    rows = list(workbook["Gaps"].iter_rows(min_row=3, values_only=True))
    assert len(rows) == 4
    unknown_date = next(row for row in rows if row[5] == "Source date unknown")
    assert unknown_date[-3:] == ("Checked with service", "Service desk", "Resolved")


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
