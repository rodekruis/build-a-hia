"""Client for the Azure AI Foundry model deployment, returning Pydantic-validated output."""

import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import BaseModel

from .settings import Settings

logger = logging.getLogger(__name__)

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


class ModelError(Exception):
    """A user-facing model failure. Messages must not include prompts or responses.

    Args:
        code: Short machine-readable failure code, safe to log (for example ``"timeout"``).
        message: User-facing explanation, safe to show in the UI.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ModelResult[T: BaseModel]:
    """Validated structured output of one model call.

    Attributes:
        parsed: The response, validated against the requested Pydantic schema.
        tokens: Total tokens reported by the deployment, or 0 if usage was not reported.
    """

    parsed: T
    tokens: int


class ModelClient(Protocol):
    """A chat model that returns structured output validated against a Pydantic schema."""

    def parse[T: BaseModel](
        self, messages: list[dict[str, str]], schema: type[T]
    ) -> ModelResult[T]:
        """Send chat messages and parse the answer into ``schema``.

        Args:
            messages: Chat messages with ``role`` and ``content`` keys.
            schema: Pydantic model the answer must conform to.

        Returns:
            The parsed answer and the number of tokens used.

        Raises:
            ModelError: If the model call fails or returns no usable answer.
        """
        ...


def model_configured(settings: Settings) -> bool:
    """Return whether a model endpoint and deployment are configured."""
    return bool(settings.foundry_endpoint and settings.foundry_deployment)


def _base_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if not base.endswith("/openai/v1"):
        base = f"{base}/openai/v1"
    return f"{base}/"


class FoundryModelClient:
    """Model client for an Azure AI Foundry deployment via its OpenAI-compatible API.

    Authenticates with the configured API key, or with Microsoft Entra ID through
    ``DefaultAzureCredential`` when no key is set.

    Args:
        settings: Application settings with the Foundry endpoint, deployment and limits.

    Raises:
        ModelError: If the endpoint or deployment is not configured.
    """

    def __init__(self, settings: Settings) -> None:
        if not (settings.foundry_endpoint and settings.foundry_deployment):
            raise ModelError("not_configured", "The AI model is not configured.")
        from openai import OpenAI

        api_key: Any = settings.foundry_api_key
        if not api_key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            api_key = get_bearer_token_provider(DefaultAzureCredential(), COGNITIVE_SERVICES_SCOPE)
        self._client = OpenAI(
            base_url=_base_url(settings.foundry_endpoint),
            api_key=api_key,
            timeout=settings.model_timeout_seconds,
            max_retries=2,
        )
        self._deployment = settings.foundry_deployment
        self._max_output_tokens = settings.model_max_output_tokens

    def parse[T: BaseModel](
        self, messages: list[dict[str, str]], schema: type[T]
    ) -> ModelResult[T]:
        """Send chat messages and parse the answer into ``schema``.

        Output is capped at the configured maximum output tokens. Errors are mapped to
        ``ModelError`` codes without including prompt or response content.

        Args:
            messages: Chat messages with ``role`` and ``content`` keys.
            schema: Pydantic model the answer must conform to.

        Returns:
            The parsed answer and the total tokens reported by the deployment.

        Raises:
            ModelError: If the request fails, is filtered, is cut off, is refused or the
                answer cannot be parsed.
        """
        import openai

        try:
            completion = self._client.chat.completions.parse(
                model=self._deployment,
                messages=cast(Any, messages),
                response_format=schema,
                max_completion_tokens=self._max_output_tokens,
            )
        except openai.LengthFinishReasonError as error:
            raise ModelError(
                "output_too_long", "The model's answer was cut off. Try again."
            ) from error
        except openai.ContentFilterFinishReasonError as error:
            raise ModelError(
                "content_filter", "The model's content filter blocked this request."
            ) from error
        except openai.RateLimitError as error:
            raise ModelError("rate_limited", "The AI model is busy. Try again later.") from error
        except openai.APITimeoutError as error:
            raise ModelError("timeout", "The AI model took too long to respond.") from error
        except (openai.AuthenticationError, openai.PermissionDeniedError) as error:
            raise ModelError("auth", "The app could not sign in to the AI model.") from error
        except openai.APIConnectionError as error:
            raise ModelError("unreachable", "The AI model could not be reached.") from error
        except openai.APIStatusError as error:
            logger.warning("Model request failed with HTTP %s", error.status_code)
            raise ModelError("model_error", "The AI model returned an error.") from error

        message = completion.choices[0].message
        if message.refusal:
            raise ModelError("refused", "The AI model declined this request.")
        if message.parsed is None:
            raise ModelError("invalid_output", "The AI model returned an unusable answer.")
        tokens = completion.usage.total_tokens if completion.usage else 0
        return ModelResult(parsed=message.parsed, tokens=tokens)
