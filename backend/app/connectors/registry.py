"""Connector registry. Connectors self-register with @register at import time; see discover()."""

from __future__ import annotations

import re

from app.connectors.base import Capability, Connector

_REGISTRY: dict[str, type[Connector]] = {}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


class UnknownConnectorError(KeyError):
    pass


def register[C: type[Connector]](cls: C) -> C:
    desc = cls.describe()
    if not _KEY_RE.match(desc.key):
        raise ValueError(f"invalid connector key {desc.key!r}")
    if desc.key in _REGISTRY and _REGISTRY[desc.key] is not cls:
        raise RuntimeError(f"duplicate connector key {desc.key!r}")
    if Capability.POLL in desc.capabilities and not desc.poll_interval_s:
        raise ValueError(f"{desc.key}: POLL capability requires poll_interval_s")
    _REGISTRY[desc.key] = cls
    return cls


def get(key: str) -> type[Connector]:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise UnknownConnectorError(key) from None


def all_connectors() -> dict[str, type[Connector]]:
    return dict(sorted(_REGISTRY.items()))
