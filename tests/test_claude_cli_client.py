"""The Claude Code CLI gateway: executable resolution, argv isolation, stdin,
environment, JSON parsing and error mapping. The subprocess seam is faked -
nothing here spawns a real `claude`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import integrations.claude_cli.client as cli
from exceptions.claude_cli import ClaudeCliError, ClaudeCliNotConfigured, ClaudeCliResponseError

EXE = "C:/fake/claude.exe"


class FakeRun:
    """Stands in for run_cli: records each call, then returns or raises.

    Given several outcomes, they are used in order; the last one repeats."""

    def __init__(self, *outcomes: cli.CliResult | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, prompt, *, cwd, env, timeout_s):
        self.calls.append({"argv": argv, "prompt": prompt, "cwd": cwd, "env": env, "timeout_s": timeout_s})
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok(result: str, **extra: object) -> cli.CliResult:
    body: dict[str, object] = {
        "type": "result", "subtype": "success", "is_error": False, "result": result,
        "duration_ms": 1234, "num_turns": 1, "total_cost_usd": 0.0,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    body.update(extra)
    return cli.CliResult(0, json.dumps(body), "")


def _svc(monkeypatch: pytest.MonkeyPatch, *outcomes: cli.CliResult | Exception, **kwargs: object):
    monkeypatch.setattr(cli, "resolve_executable", lambda configured: EXE if configured else None)
    defaults: dict[str, object] = {"path": "claude", "model": "", "effort": "", "timeout_s": 5.0}
    defaults.update(kwargs)
    svc = cli.ClaudeCliService(**defaults)  # type: ignore[arg-type]
    fake = FakeRun(*outcomes)
    svc._run = fake
    return svc, fake


# --- the run itself ---------------------------------------------------------------------


async def test_a_run_is_isolated_and_the_prompt_travels_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, fake = _svc(monkeypatch, _ok("# Report"))
    prompt = "JSON DATA: " + "x" * 70_000  # far past any Windows argv limit
    out = await svc.generate_text(prompt=prompt, system_instruction="You audit.")
    assert out == "# Report"
    call = fake.calls[0]
    argv = call["argv"]
    assert argv[0] == EXE
    for flag in ("--print", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    # --bare would read auth only from ANTHROPIC_API_KEY (no OAuth login) and
    # --restricted would ignore the user's settings; neither may be used.
    assert "--bare" not in argv and "--restricted" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--tools") + 1] == ""  # every tool disabled
    assert argv[argv.index("--system-prompt") + 1] == "You audit."
    assert "--model" not in argv and "--effort" not in argv  # the CLI's own defaults
    assert call["prompt"] == prompt and prompt not in " ".join(argv)
    assert call["timeout_s"] == 5.0


async def test_model_and_effort_are_passed_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, fake = _svc(monkeypatch, _ok("ok"), model="sonnet", effort="high")
    await svc.generate_text(prompt="p", system_instruction="s")
    argv = fake.calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "high"


async def test_the_run_gets_a_fixed_empty_scratch_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, fake = _svc(monkeypatch, _ok("ok"))
    await svc.generate_text(prompt="p", system_instruction="s")
    cwd = Path(str(fake.calls[0]["cwd"]))
    assert cwd == cli._SCRATCH_DIR and cwd.is_dir()
    assert cwd.resolve() != Path(os.getcwd()).resolve()  # never the repo: no CLAUDE.md, no .claude/
    assert not any(cwd.iterdir())  # nothing for the CLI to read there


async def test_the_parent_claude_session_is_hidden_from_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_PID", "123")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "C:/keep/me")  # where the login lives: must survive
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")  # a credential, not session plumbing
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shell-exported")  # would flip billing to the key
    svc, fake = _svc(monkeypatch, _ok("ok"))
    await svc.generate_text(prompt="p", system_instruction="s")
    env = fake.calls[0]["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env and "CLAUDE_PID" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "C:/keep/me"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["DISABLE_AUTOUPDATER"] == "1" and env["DISABLE_TELEMETRY"] == "1"
    assert "PATH" in env or "Path" in env


async def test_a_wrapping_markdown_fence_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, _ok("```markdown\n# Report\n\nBody\n```"))
    assert await svc.generate_text(prompt="p", system_instruction="s") == "# Report\n\nBody"


async def test_a_list_of_events_is_accepted_and_the_result_item_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [{"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success", "is_error": False, "result": "# R"}]
    svc, _ = _svc(monkeypatch, cli.CliResult(0, json.dumps(events), ""))
    assert await svc.generate_text(prompt="p", system_instruction="s") == "# R"


# --- failures ---------------------------------------------------------------------------


async def test_not_installed_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "resolve_executable", lambda configured: None)
    svc = cli.ClaudeCliService(path="claude", model="", effort="", timeout_s=5.0)
    assert not svc.is_configured
    with pytest.raises(ClaudeCliNotConfigured, match="run `claude` once"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_an_is_error_result_surfaces_the_clis_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, _ok("You have hit your usage limit until 3pm", is_error=True, subtype="error_during_execution"))
    with pytest.raises(ClaudeCliError, match="usage limit until 3pm.*subscription"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_an_errors_list_is_surfaced_when_there_is_no_result_text(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, _ok("", is_error=True, subtype="error_during_execution", errors=["API 529 overloaded"]))
    with pytest.raises(ClaudeCliError, match="API 529 overloaded"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_a_max_tokens_stop_still_returns_the_text(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, _ok("# Partial", stop_reason="max_tokens"))
    assert await svc.generate_text(prompt="p", system_instruction="s") == "# Partial"


async def test_a_non_success_subtype_is_an_error_even_without_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, _ok("", subtype="error_max_turns", errors=["Reached maximum number of turns (1)"]))
    with pytest.raises(ClaudeCliError, match="maximum number of turns"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_a_login_refresh_race_is_retried_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interactive sessions refresh the shared login token; a run in that window fails transiently."""
    monkeypatch.setattr(cli, "_REFRESH_RACE_WAIT_S", 0.0)
    race = _ok("Could not refresh your login because another Claude Code process is refreshing it", is_error=True)
    svc, fake = _svc(monkeypatch, race, _ok("# Report"))
    assert await svc.generate_text(prompt="p", system_instruction="s") == "# Report"
    assert len(fake.calls) == 2


async def test_a_second_refresh_race_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_REFRESH_RACE_WAIT_S", 0.0)
    svc, fake = _svc(monkeypatch, _ok("another Claude Code process is refreshing it", is_error=True))
    with pytest.raises(ClaudeCliError, match="refreshing"):
        await svc.generate_text(prompt="p", system_instruction="s")
    assert len(fake.calls) == 2  # tried twice, no more


async def test_a_login_problem_gets_the_login_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, cli.CliResult(1, "", "Not logged in. Please run /login"))
    with pytest.raises(ClaudeCliError, match="exited with status 1.*log in first"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_a_crash_with_no_json_reports_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, cli.CliResult(2, "", "TypeError: boom\n    at cli.js:1"))
    with pytest.raises(ClaudeCliError, match="status 2.*TypeError: boom"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_non_json_stdout_on_success_is_a_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, cli.CliResult(0, "just some text", ""))
    with pytest.raises(ClaudeCliResponseError):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_an_empty_result_is_a_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, _ok("   "))
    with pytest.raises(ClaudeCliResponseError):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_an_empty_result_reports_the_stderr_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown --model yields a hollow success object on stdout and the reason on stderr."""
    empty = _ok("")
    svc, _ = _svc(monkeypatch, cli.CliResult(0, empty.stdout, '[claude-code:unrecognized_model] {"model":"upos"}'))
    with pytest.raises(ClaudeCliResponseError, match="unrecognized_model"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_a_timeout_is_reported_with_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, subprocess.TimeoutExpired(cmd="claude", timeout=5.0))
    with pytest.raises(ClaudeCliError, match="did not finish within 5s"):
        await svc.generate_text(prompt="p", system_instruction="s")


async def test_a_launch_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _ = _svc(monkeypatch, OSError("Access is denied"))
    with pytest.raises(ClaudeCliError, match="could not be started.*Access is denied"):
        await svc.generate_text(prompt="p", system_instruction="s")


# --- finding the executable -------------------------------------------------------------


def test_an_npm_shim_is_resolved_to_the_exe_it_wraps(tmp_path: Path) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@ECHO off\r\n", encoding="utf-8")
    real = tmp_path / cli._NPM_EXE
    real.parent.mkdir(parents=True)
    real.write_bytes(b"MZ")
    assert cli.resolve_executable(str(shim)) == str(real)


def test_a_shim_without_the_exe_is_run_as_is(tmp_path: Path) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@ECHO off\r\n", encoding="utf-8")
    assert cli.resolve_executable(str(shim)) == str(shim)


def test_a_bare_command_name_is_looked_up_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(exe) if name == "claude" else None)
    assert cli.resolve_executable("claude") == str(exe)
    assert cli.resolve_executable("not-installed") is None
    assert cli.resolve_executable("") is None


def test_a_missing_explicit_path_is_not_configured(tmp_path: Path) -> None:
    assert cli.resolve_executable(str(tmp_path / "nope" / "claude.exe")) is None


# --- the real subprocess plumbing, with a Python stub instead of the CLI ------------------

STUB_ECHO = """\
import json, sys
raw = sys.stdin.buffer.read()
sys.stdout.buffer.write(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "got %d bytes, cr=%s, text=%s" % (len(raw), b"\\r" in raw, raw.decode("utf-8")),
}, ensure_ascii=False).encode("utf-8"))
"""

STUB_SLEEP = "import time\ntime.sleep(30)\n"


def test_run_cli_sends_utf8_bytes_without_crlf_and_reads_utf8_back(tmp_path: Path) -> None:
    stub = tmp_path / "stub.py"
    stub.write_text(STUB_ECHO, encoding="utf-8")
    prompt = "line one\nline two — café 🚀"
    result = cli.run_cli([sys.executable, str(stub)], prompt, cwd=str(tmp_path), env=dict(os.environ), timeout_s=30)
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["result"] == f"got {len(prompt.encode('utf-8'))} bytes, cr=False, text={prompt}"


def test_run_cli_kills_a_hung_child_promptly_on_timeout(tmp_path: Path) -> None:
    """If the tree kill failed, communicate() would sit on the open pipes for the full 30s sleep."""
    stub = tmp_path / "sleep.py"
    stub.write_text(STUB_SLEEP, encoding="utf-8")
    started = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        cli.run_cli([sys.executable, str(stub)], "p", cwd=str(tmp_path), env=dict(os.environ), timeout_s=1.0)
    assert time.perf_counter() - started < 10
