# e2open-freight-bidder

Automates the mechanical part of bidding on freight spot-market loads through
e2open's carrier TMS portal. A human still decides **whether** to bid and **at
what rate** — this system handles login, scraping the load's offer form, and
submitting the offer within e2open's 30-minute bid window, with an email
confirmation step in between.

## How it works

```
 Spot-market alert email  ─►  human reads it, decides to bid
                                        │
                 forwards to intake@bids.carolinascourier.com
                 with the bid amount as a bare number on line 1
                                        │
                                        ▼
   SES (receiving)  ─►  S3 (incoming/)  ─►  Lambda (bidder)
                                        │
                                        ▼
                    Lambda replies: "Reply 'yes' to submit
                    $X for TMS ID <load>"   ── nothing submitted yet
                                        │
                    human replies "yes"  (routes back to intake@)
                                        │
                                        ▼
   Lambda ─►  e2open: login ─► scrape offer form ─► submit bid
                                        │
                                        ▼
                    Lambda emails back the result
                    (Save Successful / rejection reason)
```

Two-step confirmation is the safety gate: **no bid is submitted without an
explicit "yes"** from an allowlisted sender, and the rate + load are shown for
review first. Reply "no" to cancel.

## AWS resources (all in [`template.yaml`](template.yaml))

| Resource | Role |
|---|---|
| SES receipt rule (`bids-ruleset`) | Receives mail for `intake@bids.carolinascourier.com`, stores it in S3 |
| S3 bucket (`e2open-freight-bids-intake-<acct>`) | Holds the raw MIME email; `ObjectCreated` triggers the Lambda |
| Lambda (`BidderFunction`) | Parse → e2open login/scrape/submit → email back |
| Secrets Manager (`e2open/credentials`) | e2open portal login (`userID` + `password` JSON) — populated out-of-band |

The SES **domain identity** (`bids.carolinascourier.com`) and its DNS records
(MX + DKIM) are configured manually at the DNS host, since CloudFormation can't
manage third-party DNS. See [CLAUDE.md](CLAUDE.md) for the reverse-engineered
e2open request flow.

## Project layout

```
bidder/
  app.py       Lambda handler — the two-step confirm flow
  parser.py    Extract rate + TMS load ID, validate sender allowlist
  e2open.py    e2open client: login, scrape offer form, submit bid
tests/unit/    Unit tests (parser, e2open scraping, handler)
template.yaml  All AWS resources (SAM)
CLAUDE.md      Reverse-engineered e2open portal flow + open items
```

## Deploy

```bash
sam build
sam deploy                    # region + params come from samconfig.toml

# One-time after first deploy — CloudFormation can't activate a rule set:
aws ses set-active-receipt-rule-set --rule-set-name bids-ruleset --region us-east-1

# Populate the secret (run in your own shell; value never goes in git):
aws secretsmanager put-secret-value --secret-id e2open/credentials --region us-east-1 \
  --secret-string '{"userID":"<user>","password":"<pass>"}'
```

## Configuration (SAM parameters)

| Parameter | Default | Notes |
|---|---|---|
| `IntakeRecipient` | `intake@bids.carolinascourier.com` | Address SES matches; also the reply-to for confirmations |
| `Allowlist` | `vardan@ccsexpedited.com` | Comma-separated senders allowed to trigger a bid |
| `MailFrom` | `bidder@bids.carolinascourier.com` | Confirmation sender (must be on the verified domain) |
| `DryRun` | `"true"` (template) / `"false"` (samconfig) | When `true`, scrapes + builds payload but does **not** submit |

**Safety notes**
- `DryRun` is pinned to `false` in `samconfig.toml` (live). Set it back to `true`
  and redeploy to disarm.
- SES starts in **sandbox mode** — confirmation emails only reach *verified*
  recipients until you request production access.

## Tests

```bash
python3 -m pytest tests/unit/ -q
```

Tests mock AWS and e2open; no network or credentials required. The email fixture
in `tests/fixtures/` is synthetic — no real load or third-party data.
