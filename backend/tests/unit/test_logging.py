from app.logging import REDACTED, redact_secrets


def test_redacts_secret_keys_recursively() -> None:
    event = {
        "event": "x",
        "access_token": "abc",
        "status_code": 200,
        "nested": {"refresh_token": "r", "Authorization": "Bearer z", "ok": 1},
        "items": [{"client_secret": "s"}],
        "code": "oauth-code",
    }
    out = redact_secrets(None, "info", event)
    assert out["access_token"] == REDACTED
    assert out["code"] == REDACTED
    assert out["status_code"] == 200
    assert out["nested"] == {"refresh_token": REDACTED, "Authorization": REDACTED, "ok": 1}
    assert out["items"] == [{"client_secret": REDACTED}]
