"""Flask config classes; settings are read from environment variables at import time."""

import os

MIB = 1024 * 1024


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


class Config:
    """Production defaults: secure cookies, Azure or local storage, model and size limits."""

    SECRET_KEY = os.environ.get("SECRET_KEY")
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 1800

    # "local" stores everything on disk for development; production uses "azure".
    HIA_STORAGE_BACKEND = os.environ.get("HIA_STORAGE_BACKEND", "local")
    HIA_LOCAL_STORAGE_DIR = os.environ.get("HIA_LOCAL_STORAGE_DIR", ".hia-storage")
    AZURE_STORAGE_ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    # Optional for local runs; without it, managed identity or `az login` is used.
    AZURE_STORAGE_ACCOUNT_KEY = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
    # Only for local emulators such as Azurite; Azure deployments use managed identity.
    AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    HIA_BLOB_CONTAINER = os.environ.get("HIA_BLOB_CONTAINER", "hia-temp")
    HIA_TABLE_NAME = os.environ.get("HIA_TABLE_NAME", "hiaworkflow")
    HIA_CONVERT_QUEUE = os.environ.get("HIA_CONVERT_QUEUE", "hia-convert")
    HIA_GENERATE_QUEUE = os.environ.get("HIA_GENERATE_QUEUE", "hia-generate")

    # Azure AI Foundry model deployment, used from step 4 (structure proposal) onwards.
    FOUNDRY_ENDPOINT = os.environ.get("FOUNDRY_ENDPOINT")
    FOUNDRY_DEPLOYMENT = os.environ.get("FOUNDRY_DEPLOYMENT")
    # Optional for local runs; without it, managed identity or `az login` is used.
    FOUNDRY_API_KEY = os.environ.get("FOUNDRY_API_KEY")
    HIA_MODEL_TIMEOUT_SECONDS = _int_env("HIA_MODEL_TIMEOUT_SECONDS", 600)
    HIA_MODEL_MAX_OUTPUT_TOKENS = _int_env("HIA_MODEL_MAX_OUTPUT_TOKENS", 16_000)
    # Spending cap per session: prompt plus output tokens across all model requests.
    HIA_MAX_SESSION_MODEL_TOKENS = _int_env("HIA_MAX_SESSION_MODEL_TOKENS", 5_000_000)

    HIA_SESSION_IDLE_SECONDS = _int_env("HIA_SESSION_IDLE_SECONDS", 2 * 3600)
    HIA_SESSION_MAX_SECONDS = _int_env("HIA_SESSION_MAX_SECONDS", 24 * 3600)

    HIA_MAX_FILE_BYTES = _int_env("HIA_MAX_FILE_BYTES", 25 * MIB)
    HIA_MAX_SESSION_BYTES = _int_env("HIA_MAX_SESSION_BYTES", 200 * MIB)
    HIA_MAX_SOURCES = _int_env("HIA_MAX_SOURCES", 25)
    HIA_MAX_DOCUMENT_PAGES = _int_env("HIA_MAX_DOCUMENT_PAGES", 300)
    HIA_MAX_SESSION_PAGES = _int_env("HIA_MAX_SESSION_PAGES", 1000)
    HIA_MAX_URL_BYTES = _int_env("HIA_MAX_URL_BYTES", 20 * MIB)
    HIA_MAX_REDIRECTS = _int_env("HIA_MAX_REDIRECTS", 5)
    HIA_URL_TIMEOUT_SECONDS = _int_env("HIA_URL_TIMEOUT_SECONDS", 20)
    HIA_MAX_SESSION_SOURCE_TOKENS = _int_env("HIA_MAX_SESSION_SOURCE_TOKENS", 300_000)

    # Must exceed the conversion job's replica timeout, so a running job keeps its message.
    HIA_JOB_VISIBILITY_SECONDS = _int_env("HIA_JOB_VISIBILITY_SECONDS", 4 * 3600)
    HIA_MAX_JOB_ATTEMPTS = _int_env("HIA_MAX_JOB_ATTEMPTS", 3)

    MAX_CONTENT_LENGTH = _int_env("HIA_MAX_REQUEST_BYTES", 110 * MIB)


class DevConfig(Config):
    """Local development: debug mode and cookies allowed over plain HTTP."""

    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestConfig(Config):
    """Tests: fixed secret key, CSRF disabled, local storage and insecure cookies."""

    TESTING = True
    SECRET_KEY = "test-secret-key"  # noqa: S105 - tests only
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_ENABLED = False
    HIA_STORAGE_BACKEND = "local"
