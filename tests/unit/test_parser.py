"""Unit tests for bidder.parser, driven partly by the real forwarded email."""
import email
from email.message import EmailMessage
from pathlib import Path

import pytest

from bidder.parser import ParseError, parse_bid_email

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_forward.eml"
ALLOWLIST = ["vardan@ccsexpedited.com"]


def _make_email(from_addr: str, subject: str, body: str) -> bytes:
    """Build a minimal plain-text email for the negative/edge tests."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = "bid@bot.carolinascourier.com"
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


# --- the real email straight out of S3 ---

def test_parses_real_forwarded_email():
    req = parse_bid_email(FIXTURE.read_bytes(), ALLOWLIST)
    assert req.sender == "vardan@ccsexpedited.com"
    assert req.load_id == "208803999"
    assert req.rate == 100000.0
    assert req.raw_rate_line == "100000"


# --- rate parsing / validation ---

@pytest.mark.parametrize("line,expected", [
    ("100000", 100000.0),
    ("1234.56", 1234.56),
    ("1,234.56", 1234.56),
    ("$2500", 2500.0),
    ("$ 2,500.00", 2500.0),
])
def test_accepts_valid_rate_formats(line, expected):
    body = f"{line}\n\nForwarded: TMS ID 208803999\n"
    req = parse_bid_email(_make_email("vardan@ccsexpedited.com", "Fwd: TMS ID 208803999", body), ALLOWLIST)
    assert req.rate == expected


@pytest.mark.parametrize("line", [
    "100000 thanks",   # trailing text
    "bid 100000",      # leading text
    "one hundred",     # words
    "-500",            # negative
    "",                # blank first line (handled as empty body)
])
def test_rejects_non_bare_number_first_line(line):
    body = f"{line}\n\nTMS ID 208803999\n"
    with pytest.raises(ParseError):
        parse_bid_email(_make_email("vardan@ccsexpedited.com", "Fwd: TMS ID 208803999", body), ALLOWLIST)


def test_rejects_zero_rate():
    body = "0\n\nTMS ID 208803999\n"
    with pytest.raises(ParseError, match="range"):
        parse_bid_email(_make_email("vardan@ccsexpedited.com", "Fwd", body), ALLOWLIST)


def test_rejects_rate_at_or_above_max():
    body = "1000000000000\n\nTMS ID 208803999\n"
    with pytest.raises(ParseError, match="range"):
        parse_bid_email(_make_email("vardan@ccsexpedited.com", "Fwd", body), ALLOWLIST)


# --- sender allowlist ---

def test_rejects_sender_not_on_allowlist():
    body = "100000\n\nTMS ID 208803999\n"
    with pytest.raises(ParseError, match="allowlist"):
        parse_bid_email(_make_email("stranger@evil.com", "Fwd", body), ALLOWLIST)


def test_sender_match_is_case_insensitive():
    body = "100000\n\nTMS ID 208803999\n"
    req = parse_bid_email(_make_email("Vardan@CCSexpedited.com", "Fwd", body), ALLOWLIST)
    assert req.sender == "vardan@ccsexpedited.com"


# --- TMS ID ---

def test_rejects_missing_tms_id():
    body = "100000\n\nno load number here\n"
    with pytest.raises(ParseError, match="TMS ID"):
        parse_bid_email(_make_email("vardan@ccsexpedited.com", "Fwd", body), ALLOWLIST)


def test_finds_tms_id_in_body_when_not_in_subject():
    body = "100000\n\nSpot Market Load TMS ID 555123 available\n"
    req = parse_bid_email(_make_email("vardan@ccsexpedited.com", "Fwd: a load", body), ALLOWLIST)
    assert req.load_id == "555123"
