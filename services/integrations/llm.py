"""Central LLM gateway. EVERY AI call in the app goes through LLMService.

Analyzers never import the Anthropic SDK directly - they use get_llm_service().
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from config import get_settings

logger = logging.getLogger("seo.llm")

_MAX_RETRIES = 3
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

#: rough $ per 1M tokens (input, output) - for cost logging only
_PRICING: dict[str, tuple[float, float]] = {
    "haiku": (1.0, 5.0),
    "sonnet": (3.0, 15.0),
    "opus": (15.0, 75.0),
}
_DEFAULT_PRICING = (3.0, 15.0)


class LLMError(Exception):
    """Base error for LLM failures."""


class LLMNotConfiguredError(LLMError):
    """Raised when no API key is configured."""


class LLMJSONError(LLMError):
    """Raised when json_mode=True but the model did not return valid JSON."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


def _pricing_for(model: str) -> tuple[float, float]:
    for key, price in _PRICING.items():
        if key in model:
            return price
    return _DEFAULT_PRICING


def _strip_fences(text: str) -> str:
    """Remove markdown code fences and any stray prose around a JSON object."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    if not cleaned.startswith(("{", "[")):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return cleaned


class LLMService:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = settings.anthropic_api_key if api_key is None else api_key
        self.model = model or settings.llm_model
        self._client: Any = None
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> Any:
        if not self._api_key:
            raise LLMNotConfiguredError("ANTHROPIC_API_KEY is not set")
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self._api_key, max_retries=0)
        return self._client

    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> str:
        """One completion. With json_mode=True the returned string is guaranteed parseable JSON."""
        if json_mode:
            directive = "Respond with ONLY a valid JSON object - no prose, no markdown fences."
            system = f"{system}\n\n{directive}" if system else directive
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        response = await self._create_with_retries(kwargs)
        self._record_usage(response)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()
        if json_mode:
            text = _strip_fences(text)
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMJSONError(f"Model did not return valid JSON: {exc}", raw=text) from exc
        return text

    async def complete_json(self, prompt: str, system: str | None = None, max_tokens: int = 1024) -> Any:
        """Convenience wrapper: complete(json_mode=True), parsed into Python data."""
        return json.loads(
            await self.complete(prompt, system=system, max_tokens=max_tokens, json_mode=True)
        )

    async def _create_with_retries(self, kwargs: dict[str, Any]) -> Any:
        import anthropic

        client = self._get_client()
        delay = 1.0
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await client.messages.create(**kwargs)
            except anthropic.APIError as exc:
                status = getattr(exc, "status_code", None)
                retryable = isinstance(exc, anthropic.APIConnectionError) or status in _RETRYABLE_STATUS
                if not retryable or attempt == _MAX_RETRIES:
                    raise LLMError(f"LLM request failed: {exc}") from exc
                logger.warning(
                    "Transient LLM error (attempt %d/%d): %s - retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise LLMError("LLM retries exhausted")  # pragma: no cover - loop always returns or raises

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        in_price, out_price = _pricing_for(self.model)
        cost = input_tokens * in_price / 1_000_000 + output_tokens * out_price / 1_000_000
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost_usd += cost
        logger.info(
            "LLM call model=%s tokens_in=%d tokens_out=%d cost=$%.4f session_total=$%.4f",
            self.model, input_tokens, output_tokens, cost, self.total_cost_usd,
        )


_service: LLMService | None = None


def get_llm_service() -> LLMService:
    global _service
    if _service is None:
        _service = LLMService()
    return _service
