"""Central Ollama gateway. Every local-model call goes through OllamaService.

The local counterpart of integrations/google/gemini.py, with the same shape: a
small service class, a get_ollama_service() accessor, an `is_configured` gate so
routes answer 503 instead of crashing, and one `generate_text` whose signature
matches Gemini's so the report services can swap engines by name.

Talks to Ollama's HTTP API (/api/chat) over httpx; no vendor SDK. Two things
about small local models are handled here so callers never see them:

* Context: Ollama's default window is 4096 tokens, and a longer prompt is
  truncated from the START - which for a report means the system instruction
  and the opening of the JSON. `num_ctx` is therefore sent on every request,
  raised per call when the prompt needs more than the configured floor.
* Thinking: qwen3 is a reasoning model, and Ollama's `think: false` switch is
  deliberately NOT sent. On the current qwen3:4b build it does not stop the
  reasoning - it only drops the <think> tags, so the reasoning lands in the
  answer as prose ("Hmm, the user wants...") and eats the whole output budget.
  Left to think, a current Ollama returns the reasoning in a separate
  `thinking` field and a clean `content`; an older Ollama leaves it inline in
  <think> tags, which are stripped here. Either way what comes back is the
  report and nothing else, at the price of the thinking tokens.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from config import get_settings
from exceptions.ollama import OllamaError, OllamaNotConfigured, OllamaResponseError
from integrations.reply_text import unwrap_fence

logger = logging.getLogger("seo.ollama")

#: A full audit report PLUS the model's thinking, which shares this budget
#: (Ollama counts both against num_predict). A reasoning model can spend a few
#: thousand tokens planning a long audit before writing a word of it.
_MAX_REPORT_TOKENS = 12288
#: Connecting is local and instant; a refusal means Ollama is not running, and
#: waiting longer than this only delays saying so.
_CONNECT_TIMEOUT_S = 10.0
#: JSON-heavy English tokenises at roughly 3-4 chars per token on Qwen's
#: vocabulary. 3 over-estimates, which is the safe direction for a window.
_CHARS_PER_TOKEN = 3
#: Room over the estimate so the model is never asked to fit exactly.
_CTX_HEADROOM = 512

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_thinking(text: str) -> str:
    """Drop any inline reasoning block; only the answer is the report."""
    return _THINK_RE.sub("", text).strip()


def _error_detail(response: httpx.Response) -> str:
    """Ollama answers {"error": "..."} - e.g. "model 'x' not found, try pulling it
    first" - which is exactly what the user needs to read."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return response.text[:300]


def _log_throughput(model: str, data: dict[str, Any]) -> None:
    """One line per reply with the token counts and rates Ollama measured.

    A local report is slow in proportion to prompt size and CPU/GPU speed; this
    is what turns "it took forever" into "23k prompt tokens at 40 tok/s".
    Durations arrive in nanoseconds. Missing fields (an older Ollama) just skip it.
    """
    try:
        p_n, p_ns = int(data.get("prompt_eval_count") or 0), int(data.get("prompt_eval_duration") or 0)
        g_n, g_ns = int(data.get("eval_count") or 0), int(data.get("eval_duration") or 0)
        total_s = int(data.get("total_duration") or 0) / 1e9
    except (TypeError, ValueError):
        return
    if not (p_n or g_n):
        return
    p_rate = p_n / (p_ns / 1e9) if p_ns else 0.0
    g_rate = g_n / (g_ns / 1e9) if g_ns else 0.0
    thinking_chars = len(str((data.get("message") or {}).get("thinking") or ""))
    logger.info(
        "Ollama %s: %d prompt tokens at %.0f tok/s, %d generated at %.1f tok/s "
        "(%d chars of thinking), %.0fs total",
        model, p_n, p_rate, g_n, g_rate, thinking_chars, total_s,
    )


class OllamaService:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        *,
        timeout_s: float | None = None,
        num_ctx: int | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (settings.ollama_base_url if base_url is None else base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout_s = settings.ollama_timeout_s if timeout_s is None else timeout_s
        self.num_ctx = settings.ollama_num_ctx if num_ctx is None else num_ctx

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _context_for(self, prompt_chars: int, max_output_tokens: int) -> int:
        """The window to request: the configured floor, or more if this prompt needs it."""
        prompt_tokens = prompt_chars // _CHARS_PER_TOKEN
        needed = prompt_tokens + max_output_tokens + _CTX_HEADROOM
        if needed > self.num_ctx:
            logger.info(
                "Ollama prompt is ~%d tokens; raising num_ctx from %d to %d for this call",
                prompt_tokens, self.num_ctx, needed,
            )
            return needed
        return self.num_ctx

    async def generate_text(
        self,
        *,
        prompt: str,
        system_instruction: str,
        temperature: float = 0.4,
        max_output_tokens: int = _MAX_REPORT_TOKENS,
    ) -> str:
        """One chat call, returning the model's text as-is (minus its reasoning).

        Same contract as GeminiService.generate_text: every failure leaves here as
        an OllamaError so a route can map it to 502, an unset base URL is
        OllamaNotConfigured (503), and an empty reply is OllamaResponseError so no
        caller ships a blank file. No retry, on purpose: there is no "high demand"
        on a local machine. If it fails, Ollama is not running, the model is not
        pulled, or the box is too slow - and each of those names itself.
        """
        if not self.base_url:
            raise OllamaNotConfigured("OLLAMA_BASE_URL is not set")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            # No `think` key: see the module docstring - on qwen3:4b the switch
            # leaks the reasoning into the answer instead of removing it.
            "options": {
                "temperature": temperature,
                "num_predict": max_output_tokens,
                "num_ctx": self._context_for(
                    len(system_instruction) + len(prompt), max_output_tokens
                ),
            },
        }
        timeout = httpx.Timeout(self.timeout_s, connect=_CONNECT_TIMEOUT_S)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=body)
        except httpx.ConnectError as exc:
            raise OllamaError(
                f"Ollama is not reachable at {self.base_url} - is `ollama serve` running?"
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(
                f"Ollama did not finish within {self.timeout_s:.0f}s; {self.model} may be "
                "too slow on this machine for a full report"
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise OllamaError(f"Ollama answered {response.status_code}: {_error_detail(response)}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OllamaResponseError(
                "Ollama returned a non-JSON reply", raw=response.text[:500]
            ) from exc
        if not isinstance(data, dict):
            raise OllamaResponseError("Ollama returned an unexpected reply shape", raw=str(data)[:500])

        content = str((data.get("message") or {}).get("content") or "")
        _log_throughput(self.model, data)
        if data.get("done_reason") == "length":
            logger.warning(
                "Ollama %s stopped at num_predict=%d; the report may be cut short",
                self.model, max_output_tokens,
            )
        text = unwrap_fence(_strip_thinking(content))
        if not text:
            raise OllamaResponseError("Ollama returned an empty reply", raw=content[:500])
        return text


_service: OllamaService | None = None


def get_ollama_service() -> OllamaService:
    global _service
    if _service is None:
        _service = OllamaService()
    return _service
