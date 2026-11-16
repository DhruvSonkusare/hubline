import logging

from app.logging import REDACTED, AccessLogRedactor, configure_logging, redact_query, redact_secrets


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


def test_redact_query_hides_oauth_code_and_state_only() -> None:
    path = "/oauth/gmail/callback?state=s1&iss=https://accounts.google.com&code=4/0Ab&scope=x"
    assert redact_query(path) == (
        f"/oauth/gmail/callback?state={REDACTED}&iss=https://accounts.google.com"
        f"&code={REDACTED}&scope=x"
    )
    assert (
        redact_query("/api/messages?status=open&cursor=abc")
        == "/api/messages?status=open&cursor=abc"
    )


def test_uvicorn_access_log_never_shows_the_oauth_code() -> None:
    configure_logging("INFO", "console")
    configure_logging("INFO", "console")  # idempotent: one filter, not two
    access = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, AccessLogRedactor) for f in access.filters) == 1
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/oauth/gmail/callback?code=secret-code&state=st", "1.1", 302),
        None,
    )
    assert access.filters[-1].filter(record)
    assert "secret-code" not in record.getMessage()
    assert f"code={REDACTED}" in record.getMessage()
