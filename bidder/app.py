"""Lambda handler: two-step, human-confirmed bidding.

Triggered by S3 ObjectCreated under incoming/ whenever SES stores an email
addressed to the intake inbox. There are two kinds of email:

  1. A FORWARDED ALERT — bare-number rate on the first line. The handler parses
     it and replies asking for confirmation:
         "Reply 'yes' to submit $X for TMS ID <load>."
     The reply carries a Reply-To of the intake address and embeds a token
     [bid LOAD=<load> RATE=<rate>] so the confirmation is self-describing.
     NOTHING is submitted at this step.

  2. A 'YES' REPLY — first line is yes/y. The handler pulls LOAD/RATE from the
     quoted token, logs into e2open, scrapes, and submits, then emails the
     result. ('no'/cancel replies are acknowledged and dropped.)

Safety: DRY_RUN (env) still gates real submission on the 'yes' step.
Only allowlisted senders are ever acted on; strangers are dropped silently.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import boto3

from bidder.e2open import E2openClient, E2openError
from bidder.parser import ParseError, message_body, parse_bid_email, sender_address

log = logging.getLogger()
log.setLevel(logging.INFO)

s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")
ses = boto3.client("ses")

ALLOWLIST = [a.strip().lower() for a in os.environ.get("ALLOWLIST", "vardan@ccsexpedited.com").split(",") if a.strip()]
SECRET_ID = os.environ.get("SECRET_ID", "e2open/credentials")
MAIL_FROM = os.environ.get("MAIL_FROM", "auto@bot.carolinascourier.com")
REPLY_TO = os.environ.get("REPLY_TO", "bid@bot.carolinascourier.com")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() == "true"
BID_COMMENT = os.environ.get("BID_COMMENT", "")
OFFER_TTL_HOURS = int(os.environ.get("OFFER_TTL_HOURS", "24"))

# Embedded in the confirmation email; survives '>' quoting on reply. Kept on one
# line so quote-wrapping doesn't split it.
BID_TOKEN_RE = re.compile(r"LOAD=(\d+)\s+RATE=([0-9]+(?:\.[0-9]+)?)")


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        try:
            _process(bucket, key)
        except Exception:
            log.exception("Unhandled error processing s3://%s/%s", bucket, key)
    return {"ok": True}


def _process(bucket: str, key: str) -> None:
    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    sender = sender_address(raw)
    if sender not in set(ALLOWLIST):
        log.warning("Ignoring email from non-allowlisted sender %r (key=%s)", sender, key)
        return

    body = message_body(raw)
    first_word = _first_word(body)

    if first_word in ("yes", "y"):
        _handle_confirmation(sender, body)
    elif first_word in ("no", "n", "cancel"):
        log.info("Cancellation reply from %s", sender)
        _reply(sender, "Freight bid cancelled", "Understood — nothing was submitted.")
    else:
        _handle_forward(sender, raw)


# --- step 1: a forwarded alert -> ask for confirmation ---------------------

def _handle_forward(sender: str, raw: bytes) -> None:
    try:
        req = parse_bid_email(raw, ALLOWLIST)
    except ParseError as e:
        log.warning("Parse failed: %s", e)
        _reply(sender, "Freight bid FAILED — could not read your request",
               f"Your forwarded email could not be processed:\n\n{e}\n\nNothing was submitted.")
        return

    log.info("Confirmation requested: load=%s rate=%.2f sender=%s", req.load_id, req.rate, sender)
    body = (
        f"Reply 'yes' to submit ${req.rate:,.2f} for TMS ID {req.load_id}.\n"
        f"Reply 'no' to cancel.\n\n"
        f"(Do not edit the line below — it identifies the bid.)\n"
        f"[bid LOAD={req.load_id} RATE={req.rate:.2f}]\n"
    )
    _reply(sender,
           f"Confirm bid: ${req.rate:,.2f} for TMS ID {req.load_id} — reply YES",
           body, reply_to=REPLY_TO)


# --- step 2: a 'yes' reply -> submit ---------------------------------------

def _handle_confirmation(sender: str, body: str) -> None:
    m = BID_TOKEN_RE.search(body)
    if not m:
        log.warning("'yes' reply from %s but no bid token found", sender)
        _reply(sender, "Freight bid — could not confirm",
               "You replied 'yes' but I couldn't find which bid to submit "
               "(the [bid LOAD=… RATE=…] line was missing from your reply). "
               "Nothing was submitted — please forward the alert again.")
        return

    load_id, rate = m.group(1), float(m.group(2))
    log.info("Confirmed submit: load=%s rate=%.2f sender=%s dry_run=%s", load_id, rate, sender, DRY_RUN)

    try:
        creds = json.loads(secrets.get_secret_value(SecretId=SECRET_ID)["SecretString"])
        client = E2openClient()
        client.login(creds["userID"], creds["password"])
        form = client.fetch_offer_form(load_id)
        exp = datetime.now() + timedelta(hours=OFFER_TTL_HOURS)
        result = client.submit_bid(
            form, rate=rate,
            expdate=exp.strftime("%m/%d/%Y"), exptime=exp.strftime("%H:%M"),
            comments=BID_COMMENT, dry_run=DRY_RUN,
        )
    except E2openError as e:
        log.error("e2open error on load %s: %s", load_id, e)
        _reply(sender, f"Freight bid FAILED — load {load_id}",
               f"Bid for load {load_id} at {rate:.2f} could not be submitted:\n\n{e}")
        return

    if result.success:
        state = "DRY RUN — not actually submitted" if result.dry_run else "SUBMITTED"
        _reply(sender, f"Freight bid {state} — load {load_id} @ {rate:.2f}",
               f"Load:   {load_id}\nRate:   {rate:.2f}\nStatus: {result.message}\n")
    else:
        _reply(sender, f"Freight bid REJECTED — load {load_id}",
               f"Load:   {load_id}\nRate:   {rate:.2f}\ne2open said: {result.message}\n")


# --- helpers ---------------------------------------------------------------

def _first_word(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s:
            return s.split()[0].lower().strip(".!,:;\"'")
    return ""


def _reply(to: str, subject: str, body: str, reply_to: str | None = None) -> None:
    if not to:
        return
    kwargs = dict(
        Source=MAIL_FROM,
        Destination={"ToAddresses": [to]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )
    if reply_to:
        kwargs["ReplyToAddresses"] = [reply_to]
    try:
        ses.send_email(**kwargs)
    except Exception:
        log.exception("Failed to send email to %s", to)
