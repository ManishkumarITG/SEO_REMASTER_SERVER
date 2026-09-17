"""Pytest bootstrap: make app/server importable no matter where pytest is invoked from."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
