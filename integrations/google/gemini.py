"""Central Gemini gateway. EVERY Gemini call in the app goes through GeminiService.

Mirrors services/integrations/llm.py: a small service class, a get_gemini_service()
accessor, an `is_configured` gate so routes can answer 503 instead of crashing, and
the vendor SDK imported lazily *inside this module only* - nothing from
`google.genai` is allowed to leak past this file.

Uses the current Google Gen AI SDK (`from google import genai`), not the deprecated
`google-generativeai` package. Structured output goes through
GenerateContentConfig(response_schema=...), which the SDK still supports; the newer
Interactions API is not used because the whole app needs one JSON-in/JSON-out call.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from config import get_settings
from exceptions.gemini import GeminiError, GeminiNotConfigured, GeminiResponseError

logger = logging.getLogger("seo.gemini")

#: google-genai takes the HTTP timeout in MILLISECONDS, not seconds.
_TIMEOUT_MS = 60_000
_MAX_OUTPUT_TOKENS = 2048

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def _strip_fences(text: str) -> str:
    """Unwrap ```json fences. response_mime_type should prevent them, but be tolerant."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    if not cleaned.startswith(("{", "[")):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return cleaned


class GeminiService:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.gemini_api_key if api_key is None else api_key
        self.model = model or settings.gemini_model
        self._client: Any = None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> Any:
        if not self._api_key:
            raise GeminiNotConfigured("GEMINI_API_KEY is not set")
        if self._client is None:
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=_TIMEOUT_MS),
            )
        return self._client

    async def generate_structured(
        self,
        *,
        prompt: str,
        system_instruction: str,
        schema: type[SchemaT],
        temperature: float = 0.7,
    ) -> SchemaT:
        """One structured call, validated against `schema`.

        Retries exactly once when the reply does not validate, with a "return only
        valid JSON" nudge appended. A second failure raises GeminiResponseError -
        callers surface that to the UI rather than guessing at the content.
        """
        from google.genai import errors as genai_errors
        from google.genai import types

        client = self._get_client()
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        )

        contents = prompt
        raw = ""
        last_error: ValidationError | None = None
        for attempt in (1, 2):
            try:
                response = await client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except genai_errors.APIError as exc:
                # Never echo the API key or raw SDK internals to the caller.
                logger.warning("Gemini API error (%s): %s", type(exc).__name__, exc)
                raise GeminiError(f"Gemini request failed: {getattr(exc, 'message', exc)}") from exc
            except Exception as exc:  # noqa: BLE001 - see below
                # genai_errors.APIError covers only errors Google's API *answered*
                # with. A DNS failure, a connect/read timeout or a TLS error raises
                # httpx exceptions straight through the SDK, and those are not
                # APIError subclasses - unwrapped they leave this gateway as a bare
                # 500. Everything that leaves here is a GeminiError so routes can
                # map it to 502.
                logger.warning("Gemini call failed (%s): %s", type(exc).__name__, exc)
                raise GeminiError(f"Gemini request failed: {type(exc).__name__}") from exc

            # `.text` is a property that concatenates the parts; it returns None
            # (and can warn) when the reply carried no text at all, e.g. a safety
            # block or a stop before any token. Treat that as an unusable reply
            # and let the retry ask again rather than crashing on None.
            try:
                raw = (response.text or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Gemini reply had no readable text (%s): %s", type(exc).__name__, exc)
                raw = ""
            try:
                return schema.model_validate_json(_strip_fences(raw))
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "Gemini returned unusable JSON on attempt %d/2: %s", attempt, exc.error_count()
                )
                contents = (
                    f"{prompt}\n\nYour previous reply did not match the required schema "
                    f"({exc.error_count()} validation error(s)). Return ONLY a valid JSON "
                    "object matching the schema - no prose, no markdown fences."
                )

        raise GeminiResponseError(
            f"Gemini did not return valid JSON after two attempts: {last_error}", raw=raw
        )


_service: GeminiService | None = None


def get_gemini_service() -> GeminiService:
    global _service
    if _service is None:
        _service = GeminiService()
    return _service
