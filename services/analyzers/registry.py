"""Analyzer registry with automatic module discovery.

Any module in services/analyzers/ (other than base/registry) is imported on
first use, so a @register_analyzer-decorated class in a new file is picked up
with zero wiring.
"""

from __future__ import annotations

import importlib
import pkgutil

from services.analyzers.base import BaseAnalyzer

_REGISTRY: dict[str, type[BaseAnalyzer]] = {}
_discovered = False


def register_analyzer(cls: type[BaseAnalyzer]) -> type[BaseAnalyzer]:
    """Class decorator: adds the analyzer to the registry, keyed by its `name`."""
    _REGISTRY[cls.name] = cls
    return cls


def _discover() -> None:
    global _discovered
    if _discovered:
        return
    import services.analyzers as pkg

    for module_info in pkgutil.iter_modules(pkg.__path__):
        if module_info.name in {"base", "registry"} or module_info.name.startswith("_"):
            continue
        importlib.import_module(f"services.analyzers.{module_info.name}")
    _discovered = True


def get_analyzers() -> list[BaseAnalyzer]:
    """Fresh instances of every registered analyzer."""
    _discover()
    return [cls() for cls in _REGISTRY.values()]


def get_analyzer_weights() -> dict[str, float]:
    _discover()
    return {name: cls.weight for name, cls in _REGISTRY.items()}
