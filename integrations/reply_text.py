"""Small clean-ups shared by the text-generating gateways (Ollama, Claude CLI)."""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"\A```[a-zA-Z]*[ \t]*\n(.*?)\n```\s*\Z", re.DOTALL)


def unwrap_fence(text: str) -> str:
    """Models often wrap a whole Markdown reply in ```markdown despite being told not to."""
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text
