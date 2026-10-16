"""The Ollama gateway: request shape, reasoning stripped, window grown, errors mapped.

HTTP is mocked with respx; no Ollama is needed to run these.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import integrations.ollama.client as ollama
from exceptions.ollama import OllamaError, OllamaNotConfigured, OllamaResponseError

BASE = "http://ollama.test:11434"
CHAT = f"{BASE}/api/chat"


def _svc(**kwargs: object) -> ollama.OllamaService:
    # The real default floor: comfortably above the output budget + headroom, so a
    # short prompt keeps it and only a long one grows it.
    defaults: dict[str, object] = {"base_url": BASE, "model": "qwen3:4b", "timeout_s": 5.0, "num_ctx": 16384}
    defaults.update(kwargs)
    return ollama.OllamaService(**defaults)  # type: ignore[arg-type]


def _reply(content: str, **extra: object) -> httpx.Response:
    body: dict[str, object] = {
        "model": "qwen3:4b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
    }
    body.update(extra)
    return httpx.Response(200, json=body)


@respx.mock
async def test_generate_text_posts_a_chat_with_a_window_and_no_think_switch() -> None:
    route = respx.post(CHAT).mock(return_value=_reply("# Report"))
    out = await _svc().generate_text(prompt="p", system_instruction="s")
    assert out == "# Report"
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "qwen3:4b"
    assert sent["stream"] is False
    # Deliberately absent: on qwen3:4b `think: false` leaks the reasoning into
    # the answer as prose instead of removing it (see the client docstring).
    assert "think" not in sent
    assert sent["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "p"},
    ]
    assert sent["options"]["num_ctx"] == 16384
    assert sent["options"]["num_predict"] == ollama._MAX_REPORT_TOKENS


@respx.mock
async def test_a_separate_thinking_field_is_left_out_of_the_report() -> None:
    """A current Ollama parses the reasoning into `thinking`; only `content` is the report."""
    respx.post(CHAT).mock(
        return_value=httpx.Response(200, json={
            "message": {"role": "assistant", "content": "# Report", "thinking": "Hmm, the user wants an audit..."},
            "done": True, "done_reason": "stop",
        })
    )
    assert await _svc().generate_text(prompt="p", system_instruction="s") == "# Report"


@respx.mock
async def test_inline_reasoning_and_a_wrapping_fence_are_stripped() -> None:
    """An older Ollama leaves qwen3's <think> block in the text, and small models
    love to fence the whole reply. Neither belongs in the report."""
    respx.post(CHAT).mock(
        return_value=_reply("<think>\nplan the sections...\n</think>\n```markdown\n# Report\n\nBody\n```")
    )
    assert await _svc().generate_text(prompt="p", system_instruction="s") == "# Report\n\nBody"


@respx.mock
async def test_the_window_grows_to_fit_a_long_prompt() -> None:
    """Ollama truncates an over-long prompt from the START - the system
    instruction - so the window must follow the prompt, never the reverse."""
    route = respx.post(CHAT).mock(return_value=_reply("ok"))
    prompt = "x" * 60_000  # ~20k tokens at 3 chars/token, far over a 4096 floor
    await _svc(num_ctx=4096).generate_text(prompt=prompt, system_instruction="s", max_output_tokens=1000)
    sent = json.loads(route.calls.last.request.content)
    assert sent["options"]["num_ctx"] >= 60_000 // 3 + 1000


@respx.mock
async def test_a_short_prompt_keeps_the_configured_window() -> None:
    route = respx.post(CHAT).mock(return_value=_reply("ok"))
    await _svc(num_ctx=16384).generate_text(prompt="short", system_instruction="s")
    assert json.loads(route.calls.last.request.content)["options"]["num_ctx"] == 16384


@respx.mock
async def test_ollama_not_running_is_a_clear_error() -> None:
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(OllamaError, match="not reachable"):
        await _svc().generate_text(prompt="p", system_instruction="s")


@respx.mock
async def test_a_missing_model_surfaces_ollamas_own_message() -> None:
    respx.post(CHAT).mock(
        return_value=httpx.Response(404, json={"error": "model 'qwen3:4b' not found, try pulling it first"})
    )
    with pytest.raises(OllamaError, match="try pulling it first"):
        await _svc().generate_text(prompt="p", system_instruction="s")


@respx.mock
async def test_a_timeout_names_the_model_as_too_slow() -> None:
    respx.post(CHAT).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OllamaError, match="too slow"):
        await _svc().generate_text(prompt="p", system_instruction="s")


@respx.mock
async def test_an_empty_reply_is_a_response_error() -> None:
    respx.post(CHAT).mock(return_value=_reply("<think>only thoughts, no answer</think>"))
    with pytest.raises(OllamaResponseError):
        await _svc().generate_text(prompt="p", system_instruction="s")


@respx.mock
async def test_a_non_json_reply_is_a_response_error() -> None:
    respx.post(CHAT).mock(return_value=httpx.Response(200, text="<html>proxy page</html>"))
    with pytest.raises(OllamaResponseError):
        await _svc().generate_text(prompt="p", system_instruction="s")


@respx.mock
async def test_a_reply_cut_off_by_length_is_still_returned() -> None:
    respx.post(CHAT).mock(return_value=_reply("# Partial", done_reason="length"))
    assert await _svc().generate_text(prompt="p", system_instruction="s") == "# Partial"


async def test_a_blank_base_url_is_not_configured() -> None:
    svc = _svc(base_url="")
    assert not svc.is_configured
    with pytest.raises(OllamaNotConfigured):
        await svc.generate_text(prompt="p", system_instruction="s")


def test_a_trailing_slash_on_the_base_url_is_tolerated() -> None:
    assert _svc(base_url=f"{BASE}/").base_url == BASE
