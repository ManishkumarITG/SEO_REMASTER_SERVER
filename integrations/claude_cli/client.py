"""Central Claude Code CLI gateway. Every report written by Claude goes through here.

The third engine, and the odd one out: there is no API key and no HTTP call.
The user is logged into the Claude Code CLI (`claude`) on this machine and wants
reports written on that subscription, so this service runs the CLI in headless
print mode as a subprocess and reads back its JSON result. Same shape as the
Gemini and Ollama gateways (service class, `is_configured`, `generate_text`,
singleton accessor, NotConfigured / Error / ResponseError) so
services/report_engines.py can switch engines by name.

Three things about driving a CLI are handled here so callers never see them:

* The executable. An npm install on Windows puts a `claude.cmd` shim on PATH
  that execs `node_modules/@anthropic-ai/claude-code/bin/claude.exe`. Running
  the shim means cmd.exe re-parses our arguments and a timeout kills only the
  shim, leaving the real process running. So the shim is resolved to that exe
  when it exists, and the real executable is run directly.
* Isolation. A report is a plain one-shot text generation, so the run gets
  `--tools ""` (no tools), `--safe-mode` (no CLAUDE.md, skills, plugins, hooks
  or MCP servers - while auth and the user's model choice work normally),
  `--strict-mcp-config` (belt and braces: no MCP servers at all),
  `--system-prompt` (replaces the coding-agent persona), `--no-session-persistence`
  (nothing written under ~/.claude), a scratch cwd (no project CLAUDE.md), and
  the parent session's CLAUDE_CODE_* variables stripped so it also works when
  the API server was started from inside a Claude Code terminal. NOT `--bare`:
  in that mode the CLI reads auth only from ANTHROPIC_API_KEY, never the OAuth
  login, so a subscription user would get an auth failure. NOT `--restricted`:
  it ignores the user's settings file, i.e. their chosen model.
* The prompt. It is up to ~70 KB and Windows caps a command line near 32 KB, so
  it always goes on stdin - as UTF-8 bytes, because a text-mode pipe on Windows
  would rewrite every newline as CRLF. Only the ~1.5 KB system instruction rides
  on argv.
* Billing. A shell-exported ANTHROPIC_API_KEY would make the CLI bill that key
  instead of the login, silently. It is stripped: this engine always means
  "the account `claude` is logged in as", which is the whole point of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import get_settings
from exceptions.claude_cli import ClaudeCliError, ClaudeCliNotConfigured, ClaudeCliResponseError
from integrations.reply_text import unwrap_fence

logger = logging.getLogger("seo.claude_cli")

#: Signature parity with the other engines only. The CLI exposes no output cap,
#: so the value is accepted and ignored: the model decides when the report is done.
_MAX_REPORT_TOKENS = 16384
#: Variables a running Claude Code session exports to its children (passed on,
#: they make the CLI believe it is nested inside that session), plus the API-key
#: variables that would change who pays (see the module docstring).
_STRIP_ENV_PREFIXES = ("CLAUDE_CODE_",)
_STRIP_ENV_NAMES = frozenset({
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT", "TRACEPARENT",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
})
#: The one CLAUDE_CODE_* variable that is a credential, not session plumbing: a
#: headless login token some deployments rely on. Always passed through.
_KEEP_ENV_NAMES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
#: A headless report run is no place for an auto-update mid-run, usage
#: telemetry or cost warnings on stderr.
_CHILD_ENV_EXTRA = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_COST_WARNINGS": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
#: The real executable an npm `claude.cmd` shim wraps, relative to the shim.
_NPM_EXE = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
#: One fixed, empty working directory for every run: no project CLAUDE.md or
#: .claude/ settings can apply there, and whatever per-directory state the CLI
#: keeps lands under a single slug instead of one per report.
_SCRATCH_DIR = Path(tempfile.gettempdir()) / "seo-remaster-claude-cli"
#: How much of a failure's stderr/stdout is worth showing the user.
_TAIL_CHARS = 600
#: Interactive sessions on the same machine refresh the shared login token now
#: and then; a run that lands in that window fails with a "refreshing" message
#: and simply works a moment later.
_REFRESH_RACE_MARKER = "refreshing"
_REFRESH_RACE_WAIT_S = 15.0

#: Reports are heavy, minutes-long generations on one person's subscription;
#: two at once would only race each other, so runs are serialised per process.
_ONE_AT_A_TIME = asyncio.Lock()


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


def resolve_executable(configured: str) -> str | None:
    """The absolute path to run, or None when the CLI is not installed.

    `configured` is a command name looked up on PATH ("claude") or a path. An
    npm `.cmd` / `.bat` shim is replaced by the claude.exe it wraps when that
    file is present, for the reasons in the module docstring.
    """
    if not configured:
        return None
    if os.path.isabs(configured) or os.sep in configured or "/" in configured:
        candidate: str | None = configured if os.path.exists(configured) else None
    else:
        candidate = shutil.which(configured)
    if candidate is None:
        return None
    path = Path(candidate)
    if path.suffix.lower() in {".cmd", ".bat"}:
        real = path.parent / _NPM_EXE
        if real.exists():
            return str(real)
    return str(path)


def child_env() -> dict[str, str]:
    """The parent environment minus the parent Claude Code session's own markers.

    Everything else - PATH, HOME, and CLAUDE_CONFIG_DIR in particular, which is
    where the login lives - is passed through untouched.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name in _KEEP_ENV_NAMES
        or (name not in _STRIP_ENV_NAMES and not name.startswith(_STRIP_ENV_PREFIXES))
    }
    env.update(_CHILD_ENV_EXTRA)
    return env


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Stop the CLI and anything it started.

    Popen.kill() alone leaves a Windows child process running when the parent
    was a shim, so on Windows the whole tree is killed by PID.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - already killed; nothing more to do
        pass


def run_cli(
    argv: list[str], prompt: str, *, cwd: str, env: dict[str, str], timeout_s: float
) -> CliResult:
    """Blocking: run the CLI once with `prompt` on stdin.

    The subprocess seam - tests replace it on the service instance. Raises
    subprocess.TimeoutExpired (after killing the process tree) and OSError
    (the executable could not be started); the caller maps both. Pipes are
    binary on purpose: a text-mode stdin on Windows rewrites every "\\n" in the
    prompt as "\\r\\n". The child keeps the server's console, so Ctrl+C on
    uvicorn still reaches it.
    """
    proc = subprocess.Popen(  # noqa: S603 - argv is built from settings and constants
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(prompt.encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise
    decode = lambda raw: raw.decode("utf-8", errors="replace").replace("\r\n", "\n")  # noqa: E731
    return CliResult(proc.returncode, decode(out), decode(err))


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text[-_TAIL_CHARS:] if len(text) > _TAIL_CHARS else text


def _with_hint(detail: str) -> str:
    """Attach the one-line fix for the two failures a user can act on."""
    lowered = detail.lower()
    if "log in" in lowered or "login" in lowered or "not authenticated" in lowered:
        return f"{detail} - open a terminal, run `claude`, and log in first"
    if "limit" in lowered:
        return f"{detail} - your Claude subscription's usage limit; wait, or use another engine"
    return detail


def _result_object(stdout: str) -> dict[str, Any] | None:
    """The CLI's JSON result: one object with --output-format json, tolerated as
    a list of events too (the last `result` item wins)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(data, list):
        data = next(
            (item for item in reversed(data) if isinstance(item, dict) and item.get("type") == "result"),
            None,
        )
    return data if isinstance(data, dict) else None


def _log_usage(data: dict[str, Any], model: str) -> None:
    usage = data.get("usage") or {}
    models = data.get("modelUsage")
    used = ", ".join(models) if isinstance(models, dict) and models else (model or "default")
    logger.info(
        "Claude CLI (%s): %s tokens in, %s out, %s turn(s), %.1fs, cost %s",
        used,
        usage.get("input_tokens", "?"),
        usage.get("output_tokens", "?"),
        data.get("num_turns", "?"),
        float(data.get("duration_ms") or 0) / 1000,
        data.get("total_cost_usd", "?"),
    )


class ClaudeCliService:
    def __init__(
        self,
        path: str | None = None,
        model: str | None = None,
        *,
        effort: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        settings = get_settings()
        self.path = settings.claude_cli_path if path is None else path
        self.model = settings.claude_cli_model if model is None else model
        self.effort = settings.claude_cli_effort if effort is None else effort
        self.timeout_s = settings.claude_cli_timeout_s if timeout_s is None else timeout_s
        # The subprocess seam: tests swap in a fake that never spawns anything.
        self._run = run_cli

    @property
    def is_configured(self) -> bool:
        return resolve_executable(self.path) is not None

    def _argv(self, exe: str, system_instruction: str) -> list[str]:
        argv = [
            exe,
            "--print",
            "--output-format", "json",
            "--safe-mode",
            "--strict-mcp-config",
            "--tools", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt", system_instruction,
        ]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        return argv

    async def generate_text(
        self,
        *,
        prompt: str,
        system_instruction: str,
        temperature: float = 0.4,  # noqa: ARG002 - parity with the other engines; the CLI has no such knob
        max_output_tokens: int = _MAX_REPORT_TOKENS,  # noqa: ARG002 - same
    ) -> str:
        """One headless CLI run, returning the model's text as-is.

        Same contract as the other engines: every failure leaves as a
        ClaudeCliError so a route maps it to 502, a missing executable is
        ClaudeCliNotConfigured (503), and an empty reply is ClaudeCliResponseError
        so no caller ships a blank file. The blocking subprocess runs in a worker
        thread (asyncio.to_thread) rather than an asyncio subprocess, which needs
        the Proactor loop on Windows and is not worth depending on.
        """
        exe = resolve_executable(self.path)
        if exe is None:
            raise ClaudeCliNotConfigured(
                "Claude Code CLI not found - install it and run `claude` once to log in, "
                "or point CLAUDE_CLI_PATH at the executable"
            )
        argv = self._argv(exe, system_instruction)
        _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        async with _ONE_AT_A_TIME:
            for attempt in (1, 2):
                try:
                    result = await asyncio.to_thread(
                        self._run, argv, prompt, cwd=str(_SCRATCH_DIR), env=child_env(),
                        timeout_s=self.timeout_s,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ClaudeCliError(
                        f"Claude CLI did not finish within {self.timeout_s:.0f}s"
                    ) from exc
                except OSError as exc:
                    raise ClaudeCliError(f"Claude CLI could not be started: {exc}") from exc
                try:
                    return self._text_from(result)
                except ClaudeCliError as exc:
                    if attempt == 1 and _REFRESH_RACE_MARKER in str(exc).lower():
                        logger.info("Claude CLI login is being refreshed by another process; retrying once")
                        await asyncio.sleep(_REFRESH_RACE_WAIT_S)
                        continue
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _text_from(self, result: CliResult) -> str:
        stdout, stderr = result.stdout.strip(), result.stderr.strip()
        data = _result_object(stdout) if stdout else None
        if data is None:
            if result.returncode != 0:
                raise ClaudeCliError(
                    f"Claude CLI exited with status {result.returncode}: "
                    f"{_with_hint(_tail(stderr or stdout) or 'no output')}"
                )
            raise ClaudeCliResponseError("Claude CLI returned no JSON result", raw=(stdout or stderr)[:500])
        text = str(data.get("result") or "")
        failed = data.get("is_error") or result.returncode != 0 or data.get("subtype", "success") != "success"
        if failed:
            errors = data.get("errors")
            listed = "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else ""
            detail = text or listed or _tail(stderr) or f"subtype {data.get('subtype', 'unknown')}"
            raise ClaudeCliError(f"Claude CLI reported an error: {_with_hint(detail)}")
        _log_usage(data, self.model)
        if data.get("stop_reason") == "max_tokens":
            logger.warning("Claude CLI stopped at the model's output limit; the report may be cut short")
        text = unwrap_fence(text.strip())
        if not text:
            # An empty reply usually has its real cause on stderr - e.g. an unknown
            # --model name is reported there while stdout still carries a hollow
            # success object. Say so, or the 502 is a mystery.
            cause = _tail(stderr)
            message = "Claude CLI returned an empty reply"
            if cause:
                message += f" ({_with_hint(cause)})"
            raise ClaudeCliResponseError(message, raw=stdout[:500])
        return text


_service: ClaudeCliService | None = None


def get_claude_cli_service() -> ClaudeCliService:
    global _service
    if _service is None:
        _service = ClaudeCliService()
    return _service
