"""Pick which AI writes a report, and map its failures to HTTP.

Two engines share one report pipeline: the prompt and the HTML rendering are
identical, only the model that writes the Markdown differs. This is the one
place that knows the engine names, so the report services and both report
routes stay engine-agnostic.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

from fastapi import HTTPException

from exceptions.claude_cli import ClaudeCliError, ClaudeCliNotConfigured
from exceptions.gemini import GeminiError, GeminiNotConfigured
from exceptions.ollama import OllamaError, OllamaNotConfigured
from integrations.claude_cli.client import get_claude_cli_service
from integrations.google.gemini import get_gemini_service
from integrations.ollama.client import get_ollama_service
from schemas import ReportEngine


class TextGenerator(Protocol):
    async def generate_text(self, *, prompt: str, system_instruction: str) -> str: ...


def generator_for(engine: ReportEngine) -> TextGenerator:
    """The service that writes the Markdown for this engine."""
    if engine == "ollama":
        return get_ollama_service()
    if engine == "claude":
        return get_claude_cli_service()
    return get_gemini_service()


def label_for(engine: ReportEngine) -> str:
    """How the report credits its author, in the masthead and the footer."""
    if engine == "ollama":
        return f"Ollama ({get_ollama_service().model})"
    if engine == "claude":
        model = get_claude_cli_service().model
        return f"Claude ({model})" if model else "Claude (CLI)"
    return "Gemini"


@contextmanager
def report_errors_as_http(engine: ReportEngine) -> Iterator[None]:
    """Turn an engine failure into the HTTP status the report routes promise.

    Not configured -> 503: a deployment gap, the key or URL is missing.
    Anything else -> 502: the report is generated live, so a model outage is a
    gateway failure, not ours. The gateways already strip anything sensitive
    from their messages, so the detail carries them verbatim.
    """
    try:
        yield
    except GeminiNotConfigured as exc:
        raise HTTPException(
            status_code=503, detail="Report generation is unavailable: GEMINI_API_KEY is not set."
        ) from exc
    except OllamaNotConfigured as exc:
        raise HTTPException(
            status_code=503, detail="Report generation is unavailable: OLLAMA_BASE_URL is not set."
        ) from exc
    except ClaudeCliNotConfigured as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Report generation is unavailable: the Claude Code CLI is not installed on the "
                "server. Install it, run `claude` once to log in, or set CLAUDE_CLI_PATH."
            ),
        ) from exc
    except (GeminiError, OllamaError, ClaudeCliError) as exc:
        raise HTTPException(
            status_code=502, detail=f"{label_for(engine)} could not generate the report: {exc}"
        ) from exc
