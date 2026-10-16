"""Which AI writes a report. Shared by the crawl and analysis report routes."""

from __future__ import annotations

from typing import Literal

#: `gemini` is the hosted default. `ollama` is the local model server: no quota
#: and no "high demand", written by a much smaller model. `claude` runs the
#: Claude Code CLI logged in on the server, on that account's subscription.
ReportEngine = Literal["gemini", "ollama", "claude"]
