from __future__ import annotations

import base64
import email

from app.connectors._util.mail import (
    build_reply,
    gmail_body_text,
    html_to_text,
    reply_recipient,
    reply_subject,
    strip_quoted,
)
from tests.connectors.payloads import b64


def test_html_to_text_keeps_structure_and_drops_scripts() -> None:
    html = (
        "<html><head><style>p{color:red}</style></head><body><p>Hi Dhruv,</p>"
        "<p>Please review&nbsp;the <b>contract</b> &amp; reply.</p><script>x()</script>"
        "<ul><li>one</li><li>two</li></ul></body></html>"
    )
    text = html_to_text(html)
    assert "Hi Dhruv," in text
    assert "Please review the contract & reply." in text
    assert "color" not in text
    assert "x()" not in text
    assert "one\n" in text
    assert "two" in text


def test_strip_quoted_reply_history() -> None:
    text = (
        "Sounds good, Friday works.\n\nOn Mon, 9 Nov 2026 at 10:00, Priya <p@x.example> "
        "wrote:\n> Can we meet Friday?\n> Thanks"
    )
    assert strip_quoted(text) == "Sounds good, Friday works."


def test_strip_quoted_two_line_gmail_header_and_gt_lines() -> None:
    text = "Yes.\n> old line\nOn Mon, 9 Nov 2026 at 10:00, Priya Raman\n<p@x.example> wrote:\n> q"
    assert strip_quoted(text) == "Yes."


def test_strip_quoted_never_returns_empty() -> None:
    assert strip_quoted("> only quoted") == "> only quoted"


def test_body_prefers_plain_then_html_and_ignores_attachments() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("plain wins")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
                ],
            },
            {
                "mimeType": "text/plain",
                "filename": "notes.txt",
                "body": {"attachmentId": "att1", "size": 10},
            },
        ],
    }
    assert gmail_body_text(payload) == "plain wins"
    html_only = {"mimeType": "text/html", "body": {"data": b64("<div>Only <i>html</i></div>")}}
    assert gmail_body_text(html_only) == "Only html"


def test_body_honours_charset() -> None:
    raw = "Grüße".encode("iso-8859-1")
    part = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=ISO-8859-1"}],
        "body": {"data": base64.urlsafe_b64encode(raw).decode().rstrip("=")},
    }
    assert gmail_body_text(part) == "Grüße"


def test_reply_recipient_prefers_reply_to() -> None:
    assert reply_recipient({"from": "A <a@x.example>"}) == "a@x.example"
    assert (
        reply_recipient({"from": "A <a@x.example>", "reply-to": "Team <team@x.example>"})
        == "team@x.example"
    )


def test_reply_subject() -> None:
    assert reply_subject("Contract") == "Re: Contract"
    assert reply_subject("RE: Contract") == "RE: Contract"
    assert reply_subject(None) == "Re:"


def test_build_reply_threads_correctly() -> None:
    raw = build_reply(
        from_addr="me@gmail.example",
        to_addr="priya@acme.example",
        subject="Contract",
        body="Will do by Friday.",
        in_reply_to="<abc@acme.example>",
        references="<root@acme.example>",
    )
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "priya@acme.example"
    assert msg["Subject"] == "Re: Contract"
    assert msg["In-Reply-To"] == "<abc@acme.example>"
    assert msg["References"] == "<root@acme.example> <abc@acme.example>"
    assert msg["Cc"] is None
    assert "Will do by Friday." in msg.get_payload(decode=True).decode()
