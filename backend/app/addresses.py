"""Address rules shared by the agent prefilter and the connectors (no I/O, no dependencies)."""

from __future__ import annotations

import re
from email.utils import parseaddr

# Matched against the local part only (before "@"), so a domain such as
# "notifications.example.com" never makes a human sender look automated.
_NO_REPLY_LOCAL = re.compile(
    r"(^|[^a-z])(no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|mailer[-_.]?daemon|bounces?)"
    r"([^a-z]|$)",
    re.IGNORECASE,
)


def is_no_reply_address(address: str | None) -> bool:
    """True for no-reply and app-notification senders.

    Examples: noreply@, no-reply@, do-not-reply@, notification@, notifications@,
    news-noreply@, mailer-daemon@, bounces+abc@.
    Non-email ids (e.g. Slack "U0123") are never matched.
    """
    addr = parseaddr(address or "")[1] or (address or "")
    local, at, _domain = addr.strip().rpartition("@")
    if not at or not local:
        return False
    return bool(_NO_REPLY_LOCAL.search(local))
