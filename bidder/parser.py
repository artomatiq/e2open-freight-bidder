"""Parse a forwarded e2open spot-market alert into a validated bid instruction.

The human reads an alert (from noreply@tms.blujaysolutions.net), decides to bid,
and forwards it to the intake inbox with the desired bid rate as a BARE NUMBER on
the first line of the body, e.g.:

    100000

    ---------- Forwarded message ---------
    From: <noreply@tms.blujaysolutions.net>
    Subject: Spot Market Load TMS ID 208803999 Available: CONOVER, NC ...

We extract three things and validate hard, failing loudly (ParseError) on anything
off — this drives a real money bid, so "guess and continue" is never acceptable:

  - sender   -> must be on the allowlist
  - rate     -> the bare number on the first line; 0 < rate < 1e12
  - load_id  -> matched from "TMS ID <digits>" in the subject or body
"""
from __future__ import annotations

import email
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr

# Mirrors the e2open form's own validation (see CLAUDE.md Step 2): rate must be
# >= 0 and < 1,000,000,000,000. We additionally require strictly > 0.
MAX_RATE = 1_000_000_000_000

TMS_ID_RE = re.compile(r"TMS ID (\d+)")

# The whole first line must be nothing but a number: optional leading '$', then
# either grouped thousands (1,234,567) or plain digits, with an optional decimal.
# Anchored at both ends so a line like "100000 thanks" is REJECTED, not misread.
RATE_RE = re.compile(r"^\$?\s*(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$")


class ParseError(Exception):
    """The email could not be turned into a valid, safe bid instruction."""


@dataclass(frozen=True)
class BidRequest:
    sender: str          # normalized (lowercased) sender address
    load_id: str         # e2open TMS ID, digits as a string
    rate: float          # validated bid amount
    raw_rate_line: str   # the exact first line we parsed, for the confirmation email


def sender_address(raw_bytes: bytes) -> str:
    """Return the normalized (lowercased) sender address, or '' if absent.

    Used by the handler to decide whether to bother replying at all before
    running the full parse — we don't want to email back unknown senders.
    """
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    _, addr = parseaddr(msg["From"] or "")
    return addr.strip().lower()


def message_body(raw_bytes: bytes) -> str:
    """Return the plain-text body of a raw email (used by the handler to
    classify forwards vs. 'yes' replies)."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)
    return _plain_text_body(msg)


def _plain_text_body(msg: EmailMessage) -> str:
    """Return the text/plain body, falling back to any text part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                return part.get_content()
        return ""
    return msg.get_content()


def _first_nonblank_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return ""


def parse_bid_email(raw_bytes: bytes, allowlist: list[str]) -> BidRequest:
    """Parse and validate a forwarded alert. Raises ParseError on any problem."""
    msg = email.message_from_bytes(raw_bytes, policy=policy.default)

    # --- sender allowlist (checked first: reject strangers before doing anything) ---
    _, sender_addr = parseaddr(msg["From"] or "")
    sender_addr = sender_addr.strip().lower()
    if not sender_addr:
        raise ParseError("Could not determine sender address from the From header.")
    allowed = {a.strip().lower() for a in allowlist}
    if sender_addr not in allowed:
        raise ParseError(f"Sender {sender_addr!r} is not on the allowlist.")

    body = _plain_text_body(msg)

    # --- rate: first non-blank line must be a bare number ---
    first_line = _first_nonblank_line(body)
    if not first_line:
        raise ParseError("Email body is empty; no rate found on the first line.")
    if not RATE_RE.match(first_line):
        raise ParseError(
            f"First line {first_line!r} is not a bare number "
            f"(expected e.g. '100000' or '1234.56')."
        )
    rate = float(re.sub(r"[,$\s]", "", first_line))
    if not (0 < rate < MAX_RATE):
        raise ParseError(f"Rate {rate} is outside the allowed range (0, {MAX_RATE}).")

    # --- load / TMS ID from subject or body ---
    subject = msg["Subject"] or ""
    match = TMS_ID_RE.search(subject) or TMS_ID_RE.search(body)
    if not match:
        raise ParseError("Could not find 'TMS ID <number>' in the subject or body.")
    load_id = match.group(1)

    return BidRequest(
        sender=sender_addr,
        load_id=load_id,
        rate=rate,
        raw_rate_line=first_line,
    )
