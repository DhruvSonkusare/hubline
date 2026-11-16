"""No-reply / app-notification address rule (shared by the prefilter and Gmail send)."""

from __future__ import annotations

import pytest

from app.addresses import is_no_reply_address


@pytest.mark.parametrize(
    "address",
    [
        "notification@slack.com",
        "notifications@github.com",
        "noreply@google.com",
        "no-reply@accounts.google.com",
        "no_reply@x.example",
        "do-not-reply@bank.example",
        "donotreply@bank.example",
        "news-noreply@google.com",
        "MAILER-DAEMON@googlemail.com",
        "bounces+123@mail.example",
        "Slack <notification@slack.com>",
    ],
)
def test_automated_senders(address: str) -> None:
    assert is_no_reply_address(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "priya@acme.example",
        "dhruv@notifications.example.com",  # the domain doesn't count, only the local part
        "replyall@x.example",
        "eb-T0-U0-abc@slack-mail.com",  # Slack's reply-by-email address is not "no-reply"
        "U0123ABCD",  # Slack user id, not an e-mail address
        "",
        None,
    ],
)
def test_human_or_non_email_senders(address: str | None) -> None:
    assert is_no_reply_address(address) is False
