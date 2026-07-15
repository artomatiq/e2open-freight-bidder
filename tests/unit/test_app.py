"""Unit tests for the Lambda handler (bidder.app), AWS + e2open fully mocked.

The flow is two-step: a forwarded alert gets a confirmation email (no submit);
a 'yes' reply (carrying the [bid LOAD=.. RATE=..] token) triggers the submit.
"""
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import bidder.app as app  # noqa: E402

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_forward.eml"


class FakeBody:
    def __init__(self, data): self._d = data
    def read(self): return self._d


class FakeS3:
    def __init__(self, data): self._data = data
    def get_object(self, Bucket, Key): return {"Body": FakeBody(self._data)}


class FakeSecrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps({"userID": "testuser", "password": "pw"})}


class FakeSES:
    def __init__(self): self.sent = []
    def send_email(self, **kw):
        self.sent.append({
            "to": kw["Destination"]["ToAddresses"][0],
            "subject": kw["Message"]["Subject"]["Data"],
            "body": kw["Message"]["Body"]["Text"]["Data"],
            "reply_to": kw.get("ReplyToAddresses"),
        })


class FakeResult:
    def __init__(self, success, message, dry_run):
        self.success, self.message, self.dry_run, self.payload = success, message, dry_run, {}


class FakeClient:
    last = None
    def __init__(self, *a, **k):
        self.login_args = None; self.submit_args = None; FakeClient.last = self
    def login(self, user_id, password): self.login_args = (user_id, password)
    def fetch_offer_form(self, load_id): self.load_id = load_id; return object()
    def submit_bid(self, form, rate, expdate, exptime, comments="", group="", dry_run=True):
        self.submit_args = dict(rate=rate, dry_run=dry_run)
        msg = "DRY RUN — not actually submitted" if dry_run else "Save Successful"
        return FakeResult(True, msg, dry_run)


def _event():
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": "incoming/x"}}}]}


def _raw(from_addr, body):
    return f"From: {from_addr}\r\nSubject: x\r\n\r\n{body}".encode()


@pytest.fixture
def ses(monkeypatch):
    _ses = FakeSES()
    monkeypatch.setattr(app, "secrets", FakeSecrets())
    monkeypatch.setattr(app, "ses", _ses)
    monkeypatch.setattr(app, "E2openClient", FakeClient)
    monkeypatch.setattr(app, "ALLOWLIST", ["vardan@ccsexpedited.com"])
    FakeClient.last = None
    return _ses


# --- step 1: forward -> confirmation email, no submit ---

def test_forward_sends_confirmation_and_does_not_submit(ses, monkeypatch):
    monkeypatch.setattr(app, "s3", FakeS3(FIXTURE.read_bytes()))
    app.lambda_handler(_event(), None)

    assert FakeClient.last is None            # never touched e2open
    assert len(ses.sent) == 1
    msg = ses.sent[0]
    assert msg["to"] == "vardan@ccsexpedited.com"
    assert "reply yes" in msg["subject"].lower()
    assert "208803999" in msg["subject"]
    assert "$100,000.00" in msg["body"]
    assert "LOAD=208803999 RATE=100000.00" in msg["body"]   # token present
    assert msg["reply_to"] == ["bid@bot.carolinascourier.com"]


def test_forward_parse_failure_emails_sender(ses, monkeypatch):
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", "bid 500\r\n\r\nno load number here\r\n")))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    assert "FAILED" in ses.sent[0]["subject"]


def test_forward_missing_rate_asks_for_amount(ses, monkeypatch):
    # Valid TMS ID, but the first line isn't a bare rate -> should be
    # recognized as a real alert and prompt for just the amount, not
    # bounced as a generic parse failure.
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", "bid 500\r\n\r\nTMS ID 1\r\n")))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    msg = ses.sent[0]
    assert "need the rate" in msg["subject"].lower()
    assert "1" in msg["subject"]
    assert "[bid LOAD=1]" in msg["body"]
    assert msg["reply_to"] == ["bid@bot.carolinascourier.com"]


def test_rate_reply_after_missing_rate_asks_for_confirmation(ses, monkeypatch):
    body = ("100000\r\n\r\n"
            "On Mon someone wrote:\r\n"
            "> Reply to this email with just the rate...\r\n"
            "> [bid LOAD=1]\r\n")
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", body)))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    msg = ses.sent[0]
    assert "reply yes" in msg["subject"].lower()
    assert "LOAD=1 RATE=100000.00" in msg["body"]
    assert msg["reply_to"] == ["bid@bot.carolinascourier.com"]


def test_rate_reply_still_invalid_asks_again(ses, monkeypatch):
    body = ("not a number\r\n\r\n"
            "> [bid LOAD=1]\r\n")
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", body)))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    msg = ses.sent[0]
    assert "still need a valid rate" in msg["subject"].lower()
    assert "[bid LOAD=1]" in msg["body"]


# --- step 2: 'yes' reply -> submit ---

YES_REPLY = ("yes\r\n\r\n"
             "On Mon someone wrote:\r\n"
             "> Reply 'yes' to submit $100,000.00 for TMS ID 208803999.\r\n"
             "> [bid LOAD=208803999 RATE=100000.00]\r\n")


def test_yes_reply_submits_dry_run(ses, monkeypatch):
    monkeypatch.setattr(app, "DRY_RUN", True)
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", YES_REPLY)))
    app.lambda_handler(_event(), None)

    assert FakeClient.last.login_args == ("testuser", "pw")
    assert FakeClient.last.load_id == "208803999"
    assert FakeClient.last.submit_args == {"rate": 100000.0, "dry_run": True}
    assert "DRY RUN" in ses.sent[0]["subject"]


def test_yes_reply_submits_for_real(ses, monkeypatch):
    monkeypatch.setattr(app, "DRY_RUN", False)
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", YES_REPLY)))
    app.lambda_handler(_event(), None)
    assert FakeClient.last.submit_args["dry_run"] is False
    assert "SUBMITTED" in ses.sent[0]["subject"]


def test_yes_reply_without_token_does_not_submit(ses, monkeypatch):
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", "yes please\r\n")))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    assert "could not confirm" in ses.sent[0]["subject"].lower()


def test_no_reply_cancels(ses, monkeypatch):
    monkeypatch.setattr(app, "s3", FakeS3(_raw("vardan@ccsexpedited.com", "no thanks\r\n")))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    assert "cancel" in ses.sent[0]["subject"].lower()


# --- allowlist still enforced across the board ---

def test_non_allowlisted_sender_is_dropped(ses, monkeypatch):
    monkeypatch.setattr(app, "s3", FakeS3(_raw("stranger@evil.com", YES_REPLY)))
    app.lambda_handler(_event(), None)
    assert FakeClient.last is None
    assert ses.sent == []
