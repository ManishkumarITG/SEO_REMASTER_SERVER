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
#: A full audit report is far longer than a Reddit draft, so the text path takes
#: its own, larger ceiling rather than the structured default above.
_MAX_REPORT_TOKENS = 8192
#: The text path retries a transient 5xx ("high demand"), which is common on the
#: flash models at peak. Total added wait on a bad spell is ~2s + 4s = 6s.
_TEXT_ATTEMPTS = 3
_TEXT_RETRY_BACKOFF_S = 2.0

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
        self._fallback_models = [
            m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()
        ]
        self._client: Any = None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _text_model_chain(self) -> list[str]:
        """The primary model first, then any distinct configured fallbacks.

        A report is expensive to abandon, so when the primary model is saturated the
        text path retries down this list. Fallbacks are deliberately from another
        generation, which sits in a separate capacity pool.
        """
        chain = [self.model]
        for name in self._fallback_models:
            if name and name not in chain:
                chain.append(name)
        return chain

    @staticmethod
    def _reply_text(response: Any) -> str:
        """`.text` concatenates the parts; it returns None (and can warn) when the
        reply carried no text at all - a safety block, or a stop before any token."""
        try:
            return (response.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini reply had no readable text (%s): %s", type(exc).__name__, exc)
            return ""

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

    async def generate_text(
        self,
        *,
        prompt: str,
        system_instruction: str,
        temperature: float = 0.4,
        max_output_tokens: int = _MAX_REPORT_TOKENS,
    ) -> str:
        """One free-text call, returning the model's text as-is.

        Separate from `generate_structured` because a report is prose, not JSON:
        no schema, no validation, no retry-for-shape. A transient 5xx ("model is
        experiencing high demand") IS retried with backoff, because it is
        explicitly temporary and a report is expensive to abandon; if a model stays
        overloaded past its retries the call falls back to the next model in the
        chain (a different generation, a different capacity pool). Errors are
        funnelled into GeminiError so a route maps them to 502 rather than a bare
        500, and an empty reply (a safety block, or a stop before any token) is
        surfaced as GeminiResponseError so the caller never ships a blank file.
        """
        import asyncio

        from google.genai import errors as genai_errors
        from google.genai import types

        client = self._get_client()
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        chain = self._text_model_chain()
        last_error: Exception | None = None
        for model_index, model in enumerate(chain):
            is_primary = model_index == 0
            for attempt in range(1, _TEXT_ATTEMPTS + 1):
                try:
                    response = await client.aio.models.generate_content(
                        model=model, contents=prompt, config=config
                    )
                except genai_errors.ServerError as exc:
                    # 5xx / UNAVAILABLE / 504: the API itself is momentarily
                    # overloaded, not a problem with our request. Retry, then fall
                    # back to the next model once this one's retries are spent.
                    last_error = exc
                    logger.warning(
                        "Gemini overloaded (%s) attempt %d/%d: %s",
                        model, attempt, _TEXT_ATTEMPTS, exc,
                    )
                    if attempt < _TEXT_ATTEMPTS:
                        await asyncio.sleep(_TEXT_RETRY_BACKOFF_S * attempt)
                        continue
                    break  # exhausted this model -> try the next in the chain
                except genai_errors.APIError as exc:
                    # A 4xx is our fault (bad request, quota, auth). On the primary
                    # model that is fatal; on a fallback it usually just means that
                    # model name is not available here, so move to the next one.
                    last_error = exc
                    if is_primary:
                        logger.warning("Gemini API error (%s): %s", type(exc).__name__, exc)
                        raise GeminiError(
                            f"Gemini request failed: {getattr(exc, 'message', exc)}"
                        ) from exc
                    logger.warning(
                        "Gemini fallback %s unavailable (%s): %s", model, type(exc).__name__, exc
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - httpx errors are not APIError
                    # A connect/read timeout or TLS/DNS error hits the shared
                    # endpoint, so a fallback model would fail the same way: stop.
                    last_error = exc
                    logger.warning("Gemini call failed (%s): %s", type(exc).__name__, exc)
                    raise GeminiError(f"Gemini request failed: {type(exc).__name__}") from exc

                text = self._reply_text(response)
                if not text:
                    # An empty reply is a content decision (safety block / no token),
                    # not a capacity problem, so another model would not help.
                    raise GeminiResponseError("Gemini returned an empty reply", raw="")
                if not is_primary:
                    logger.info("Gemini text generated by fallback model %s", model)
                return text

        detail = getattr(last_error, "message", None) or type(last_error).__name__
        raise GeminiError(f"Gemini request failed after {len(chain)} model(s): {detail}")

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

            # An empty reply (safety block, or a stop before any token) is unusable;
            # let the retry ask again rather than crashing on None.
            raw = self._reply_text(response)
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
