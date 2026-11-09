"""Connector plug-ins. Adding an app = adding one module here with a @register class."""

from __future__ import annotations

import importlib
import pkgutil

_FRAMEWORK_MODULES = {"base", "models", "registry"}


def discover() -> list[str]:
    """Import every connector module in this package (skips _private and framework modules)."""
    imported = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name in _FRAMEWORK_MODULES:
            continue
        importlib.import_module(f"{__name__}.{mod.name}")
        imported.append(mod.name)
    return imported
