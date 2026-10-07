"""Typed application settings shared by the web app and the workers."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Settings:
    """Immutable settings for the services, built from the Flask config keys.

    Secrets (storage key, connection string, Foundry API key) are excluded from `repr` so
    they do not end up in logs.
    """

    storage_backend: str
    local_storage_dir: str
    azure_account_name: str | None
    azure_connection_string: str | None = field(repr=False)
    azure_account_key: str | None = field(repr=False)
    blob_container: str
    table_name: str
    convert_queue: str
    generate_queue: str
    foundry_endpoint: str | None
    foundry_deployment: str | None
    foundry_api_key: str | None = field(repr=False)
    model_timeout_seconds: int
    model_max_output_tokens: int
    max_session_model_tokens: int
    session_idle_seconds: int
    session_max_seconds: int
    max_file_bytes: int
    max_session_bytes: int
    max_sources: int
    max_document_pages: int
    max_session_pages: int
    max_url_bytes: int
    max_redirects: int
    url_timeout_seconds: int
    max_session_source_tokens: int
    job_visibility_seconds: int
    max_job_attempts: int

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "Settings":
        """Build settings from a mapping of config keys, such as `app.config`.

        Args:
            config: Mapping with the keys defined on `Config`.

        Returns:
            The settings.

        Raises:
            KeyError: If a required key is missing.
        """
        return cls(
            storage_backend=config["HIA_STORAGE_BACKEND"],
            local_storage_dir=config["HIA_LOCAL_STORAGE_DIR"],
            azure_account_name=config.get("AZURE_STORAGE_ACCOUNT_NAME"),
            azure_connection_string=config.get("AZURE_STORAGE_CONNECTION_STRING"),
            azure_account_key=config.get("AZURE_STORAGE_ACCOUNT_KEY"),
            blob_container=config["HIA_BLOB_CONTAINER"],
            table_name=config["HIA_TABLE_NAME"],
            convert_queue=config["HIA_CONVERT_QUEUE"],
            generate_queue=config["HIA_GENERATE_QUEUE"],
            foundry_endpoint=config.get("FOUNDRY_ENDPOINT"),
            foundry_deployment=config.get("FOUNDRY_DEPLOYMENT"),
            foundry_api_key=config.get("FOUNDRY_API_KEY"),
            model_timeout_seconds=config["HIA_MODEL_TIMEOUT_SECONDS"],
            model_max_output_tokens=config["HIA_MODEL_MAX_OUTPUT_TOKENS"],
            max_session_model_tokens=config["HIA_MAX_SESSION_MODEL_TOKENS"],
            session_idle_seconds=config["HIA_SESSION_IDLE_SECONDS"],
            session_max_seconds=config["HIA_SESSION_MAX_SECONDS"],
            max_file_bytes=config["HIA_MAX_FILE_BYTES"],
            max_session_bytes=config["HIA_MAX_SESSION_BYTES"],
            max_sources=config["HIA_MAX_SOURCES"],
            max_document_pages=config["HIA_MAX_DOCUMENT_PAGES"],
            max_session_pages=config["HIA_MAX_SESSION_PAGES"],
            max_url_bytes=config["HIA_MAX_URL_BYTES"],
            max_redirects=config["HIA_MAX_REDIRECTS"],
            url_timeout_seconds=config["HIA_URL_TIMEOUT_SECONDS"],
            max_session_source_tokens=config["HIA_MAX_SESSION_SOURCE_TOKENS"],
            job_visibility_seconds=config["HIA_JOB_VISIBILITY_SECONDS"],
            max_job_attempts=config["HIA_MAX_JOB_ATTEMPTS"],
        )

    @classmethod
    def from_object(cls, config: object) -> "Settings":
        """Build settings from the upper-case attributes of a config class or object.

        Args:
            config: A config class such as `Config`; used by the workers, which have no app.

        Returns:
            The settings.
        """
        return cls.from_mapping(
            {name: getattr(config, name) for name in dir(config) if name.isupper()}
        )
