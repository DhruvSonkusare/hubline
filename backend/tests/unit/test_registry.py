import pytest

from app.connectors import discover, registry
from app.connectors.base import AuthType, Capability, Connector, ConnectorDescriptor
from app.connectors.models import FetchResult


def _make(key: str, caps: frozenset[Capability], interval: int | None = 60) -> type[Connector]:
    class _C(Connector):
        descriptor = ConnectorDescriptor(
            key=key,
            display_name=key,
            auth_type=AuthType.NONE,
            capabilities=caps,
            poll_interval_s=interval,
        )

        async def fetch_new(self, since: str | None) -> FetchResult:
            return FetchResult()

    return _C


def test_discover_registers_demo() -> None:
    assert "demo" in discover()
    assert registry.get("demo").describe().is_demo


def test_unknown_key() -> None:
    with pytest.raises(registry.UnknownConnectorError):
        registry.get("nope")


def test_duplicate_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    registry.register(_make("dup", frozenset({Capability.POLL})))
    with pytest.raises(RuntimeError, match="duplicate"):
        registry.register(_make("dup", frozenset({Capability.POLL})))


@pytest.mark.parametrize("key", ["Bad", "1abc", "a", "has-dash", "x" * 40])
def test_invalid_key_rejected(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    with pytest.raises(ValueError, match="invalid connector key"):
        registry.register(_make(key, frozenset()))


def test_poll_requires_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    with pytest.raises(ValueError, match="poll_interval_s"):
        registry.register(_make("nointerval", frozenset({Capability.POLL}), interval=None))
