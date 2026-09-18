"""Pytest bootstrap: put server/ on sys.path so top-level imports resolve.

Note: pytest must still be run FROM server/ - every import in the suite is
top-level (`from services...`, `import main`), not package-relative.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
