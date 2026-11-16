"""E-mail helpers: HTML -> text, quoted-reply stripping, Gmail MIME payloads, reply building.

Standard library only (html.parser, email), so no extra dependency is needed.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from html import unescape
from html.parser import HTMLParser
from typing import Any

MAX_BODY_CHARS = 50_000

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "table"}
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_QUOTE_HEADER = re.compile(
    r"^\s*(On .{0,200}wrote:|-{2,}\s*Original Message\s*-{2,}|Sent from my \w+)\s*$",
    re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """Drop the quoted history of a reply ("On ... wrote:" blocks and '>' lines)."""
    kept: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _QUOTE_HEADER.match(line) and i > 0:
            break
        # Gmail wraps long "On <date>, <name> <addr> wrote:" headers over two lines
        if (
            i + 1 < len(lines)
            and re.match(r"^\s*On .{0,200}$", line)
            and lines[i + 1].strip().endswith("wrote:")
        ):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    result = "\n".join(kept).strip()
    return result or text.strip()


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode_part(part: Mapping[str, Any]) -> str:
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    raw = b64url_decode(str(data))
    charset = "utf-8"
    for h in part.get("headers") or []:
        if str(h.get("name", "")).lower() == "content-type":
            m = re.search(r"charset=\"?([\w.-]+)", str(h.get("value", "")), re.IGNORECASE)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    parts = [part]
    for child in part.get("parts") or []:
        parts.extend(_walk(child))
    return parts


def gmail_body_text(payload: Mapping[str, Any]) -> str:
    """Prefer text/plain; else HTML converted to text. Attachments are ignored."""
    plain: list[str] = []
    html: list[str] = []
    for part in _walk(payload):
        if (part.get("body") or {}).get("attachmentId") or part.get("filename"):
            continue
        mime = str(part.get("mimeType", "")).lower()
        if mime == "text/plain":
            plain.append(_decode_part(part))
        elif mime == "text/html":
            html.append(_decode_part(part))
    if any(p.strip() for p in plain):
        text = "\n".join(plain)
    elif html:
        text = html_to_text("\n".join(html))
    else:
        text = ""
    text = text.replace("\r\n", "\n")
    return strip_quoted(text)[:MAX_BODY_CHARS]


def header_map(payload: Mapping[str, Any]) -> dict[str, str]:
    """Top-level headers, lower-cased names (first occurrence wins)."""
    out: dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = str(h.get("name", "")).lower()
        if name and name not in out:
            out[name] = str(h.get("value", ""))
    return out


def parse_sender(value: str) -> tuple[str, str | None]:
    name, addr = parseaddr(value)
    return addr.strip().lower(), (name.strip() or None)


def reply_recipient(headers: Mapping[str, str]) -> str:
    """Reply-To when present, else From (sender only; no CC / reply-all)."""
    reply_to = headers.get("reply-to", "").strip()
    if reply_to:
        addresses = [a for _, a in getaddresses([reply_to]) if a]
        if addresses:
            return addresses[0]
    return parseaddr(headers.get("from", ""))[1]


def reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "Re:"
    return subject if re.match(r"^re:", subject, re.IGNORECASE) else f"Re: {subject}"


def build_reply(
    *,
    from_addr: str,
    to_addr: str,
    subject: str | None,
    body: str,
    in_reply_to: str | None,
    references: str | None,
) -> str:
    """RFC 2822 reply, base64url-encoded for users.messages.send's `raw` field."""
    msg = EmailMessage()
    msg["From"] = formataddr((None, from_addr)) if from_addr else ""
    msg["To"] = to_addr
    msg["Subject"] = reply_subject(subject)
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
