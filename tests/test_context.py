import json
from dataclasses import asdict

from factories import CONTEXT_FORM, session_key_of

from build_a_hia import create_app
from build_a_hia.config import TestConfig
from build_a_hia.services.context import ProjectContext
from build_a_hia.services.sessions import SESSION_ROW

CONTEXT = ProjectContext(
    country="Ukraine",
    situation="Displacement",
    target_group="IDPs",
    locations="Kyiv",
    output_language="uk",
    locale_dir="ltr",
)


def test_context_page_shows_form(client):
    response = client.get("/context")

    assert response.status_code == 200
    assert b"Describe the situation" in response.data


def test_saving_context_creates_session_cookie_and_redirects(client, services):
    response = client.post("/context", data=CONTEXT_FORM)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/sources")
    cookie_header = response.headers["Set-Cookie"]
    assert "hia_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=Lax" in cookie_header

    workspace = services.sessions.get_active(session_key_of(client))
    assert workspace.context.country == "Ukraine"
    assert workspace.context.locale_language == "uk"
    assert workspace.context.locale_dir == "ltr"
    assert workspace.context.reference_date == "2026-10-01"


def test_cookie_token_is_not_stored(client, services):
    client.post("/context", data=CONTEXT_FORM)
    token = client.get_cookie("hia_session").value

    assert services.table.get(token, SESSION_ROW) is None
    assert services.table.get(session_key_of(client), SESSION_ROW) is not None


def test_rtl_language_defaults_to_rtl_direction(client, services):
    client.post("/context", data={**CONTEXT_FORM, "output_language": "ar"})

    workspace = services.sessions.get_active(session_key_of(client))
    assert workspace.context.locale_dir == "rtl"


def test_explicit_direction_overrides_language_default(client, services):
    client.post("/context", data={**CONTEXT_FORM, "output_language": "ar", "locale_dir": "auto"})

    workspace = services.sessions.get_active(session_key_of(client))
    assert workspace.context.locale_dir == "auto"


def test_language_can_be_typed_as_a_name(client, services):
    response = client.post(
        "/context", data={**CONTEXT_FORM, "output_language": "Pasjtoe"}, follow_redirects=True
    )

    workspace = services.sessions.get_active(session_key_of(client))
    assert workspace.context.output_language == "ps"
    assert workspace.context.locale_dir == "rtl"
    assert b"Output language: Pashto (ps), right to left" in response.data
    assert b'value="Pashto"' in client.get("/context").data


def test_unknown_language_codes_are_kept_with_a_warning(client, services):
    response = client.post(
        "/context", data={**CONTEXT_FORM, "output_language": "tvl"}, follow_redirects=True
    )

    workspace = services.sessions.get_active(session_key_of(client))
    assert workspace.context.output_language == "tvl"
    assert workspace.context.locale_dir == "auto"
    assert b"is not a language we know" in response.data


def test_unrecognized_language_is_rejected(client):
    response = client.post("/context", data={**CONTEXT_FORM, "output_language": "Klingonese"})

    assert response.status_code == 400
    assert b"Unknown language" in response.data
    assert client.get_cookie("hia_session") is None


def test_invalid_context_is_rejected_without_session(client):
    response = client.post("/context", data={**CONTEXT_FORM, "country": ""})

    assert response.status_code == 400
    assert client.get_cookie("hia_session") is None


def test_context_is_prefilled_on_return(workspace_client):
    response = workspace_client.get("/context")

    assert b"Dnipro, Kharkiv" in response.data


def test_updating_context_keeps_session(workspace_client, services):
    key = session_key_of(workspace_client)
    workspace_client.post("/context", data={**CONTEXT_FORM, "country": "Moldova"})

    assert session_key_of(workspace_client) == key
    workspace = services.sessions.get_active(key)
    assert workspace.context.country == "Moldova"
    assert workspace.context_version == 2


def test_contexts_saved_with_removed_fields_still_load():
    raw = json.dumps({**asdict(CONTEXT), "organisation": "Ukrainian Red Cross", "notes": "x"})

    assert ProjectContext.from_json(raw) == CONTEXT


def test_sources_page_has_no_extra_context_form(workspace_client):
    page = workspace_client.get("/sources").data

    assert b"more context" not in page
    assert workspace_client.post("/context/more", data={}).status_code in (404, 405)


def test_sources_page_requires_session(client):
    response = client.get("/sources")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/context")


def test_unknown_cookie_is_cleared(client):
    client.set_cookie("hia_session", "not-a-valid-session")

    response = client.get("/sources")

    assert response.status_code == 302
    assert "hia_session=;" in response.headers["Set-Cookie"]


def test_delete_session_removes_data_and_cookie(workspace_client, services):
    key = session_key_of(workspace_client)

    response = workspace_client.post("/session/delete")

    assert response.status_code == 302
    assert services.sessions.get_active(key) is None
    assert workspace_client.get_cookie("hia_session") is None


def test_index_offers_to_continue_existing_session(workspace_client):
    response = workspace_client.get("/")

    assert b"Continue your session" in response.data


def test_forms_require_csrf_token_when_enabled(storage_dir):
    class Config(TestConfig):
        HIA_LOCAL_STORAGE_DIR = str(storage_dir)
        WTF_CSRF_ENABLED = True

    client = create_app(Config).test_client()

    assert b'name="csrf_token"' in client.get("/context").data
    response = client.post("/context", data=CONTEXT_FORM)
    assert response.status_code == 400
    assert client.get_cookie("hia_session") is None
