from dataclasses import replace

import pytest
from factories import (
    CONTEXT_FORM,
    SOURCE_TEXTS,
    add_converted_source,
    make_text_pdf,
    session_key_of,
)

from build_a_hia import create_app
from build_a_hia.config import TestConfig
from build_a_hia.services.settings import Settings


class ModelConfig(TestConfig):
    FOUNDRY_ENDPOINT = "https://foundry.example.invalid/"
    FOUNDRY_DEPLOYMENT = "test-deployment"


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture
def app(storage_dir):
    class Config(ModelConfig):
        HIA_LOCAL_STORAGE_DIR = str(storage_dir)

    return create_app(Config)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


@pytest.fixture
def services(app):
    return app.extensions["hia_services"]


@pytest.fixture
def settings(storage_dir) -> Settings:
    return replace(Settings.from_object(ModelConfig), local_storage_dir=str(storage_dir))


@pytest.fixture
def workspace_client(client):
    response = client.post("/context", data=CONTEXT_FORM)
    assert response.status_code == 302
    return client


@pytest.fixture
def ready_client(workspace_client, services):
    """A session with one converted, approved source (chunks S1-p1-c1 and S1-p2-c1)."""
    key = session_key_of(workspace_client)
    add_converted_source(services, key, SOURCE_TEXTS)
    services.sources.approve(key, accept_failed=False)
    return workspace_client


@pytest.fixture
def pdf_bytes() -> bytes:
    return make_text_pdf(["Shelter is available at the central station.", "Call 1545."])
