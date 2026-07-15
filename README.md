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
                 forwards to bid@bot.carolinascourier.com
                 with the bid amount as a bare number on line 1
                                        │
                                        ▼
   SES (receiving)  ─►  S3 (incoming/)  ─►  Lambda (bidder)
                                        │
                                        ▼
                    Lambda replies: "Reply 'yes' to submit
                    $X for TMS ID <load>"   ── nothing submitted yet
                                        │
                    human replies "yes"  (routes back to bid@)
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
| SES receipt rule (`bids-intake`) | Added to the shared `fourkites-dispatch-rules` active rule set (owned externally, not by this stack); receives mail for `bid@bot.carolinascourier.com`, stores it in S3 |
| S3 bucket (`e2open-freight-bids-intake-<acct>`) | Holds the raw MIME email; `ObjectCreated` triggers the Lambda |
| Lambda (`BidderFunction`) | Parse → e2open login/scrape/submit → email back |
| Secrets Manager (`e2open/credentials`) | e2open portal login (`userID` + `password` JSON) — populated out-of-band |

The SES **domain identity** (`bot.carolinascourier.com`) and its DNS records
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

# Populate the secret (run in your own shell; value never goes in git):
aws secretsmanager put-secret-value --secret-id e2open/credentials --region us-east-1 \
  --secret-string '{"userID":"<user>","password":"<pass>"}'
```

## Configuration (SAM parameters)

| Parameter | Default | Notes |
|---|---|---|
| `IntakeRecipient` | `bid@bot.carolinascourier.com` | Address SES matches; also the reply-to for confirmations |
| `Allowlist` | `vardan@ccsexpedited.com` | Comma-separated senders allowed to trigger a bid |
| `MailFrom` | `auto@bot.carolinascourier.com` | Confirmation sender (must be on the verified domain) |
| `SharedRuleSetName` | `fourkites-dispatch-rules` | The SES active receipt rule set this stack adds its rule to — shared with other apps on this domain, created/activated outside this stack |
| `DryRun` | `"true"` (template) / `"false"` (samconfig) | When `true`, scrapes + builds payload but does **not** submit |

**Safety notes**
- `DryRun` is pinned to `false` in `samconfig.toml` (live). Set it back to `true`
  and redeploy to disarm.
- SES starts in **sandbox mode** — confirmation emails only reach *verified*
  recipients until you request production access.
- SES runs exactly **one active rule set per account/region**, shared across every
  app on this domain (currently `fourkites-dispatch-rules`, owned by the
  dispatcher app). This stack must never create or activate a rule set of its
  own — doing so would deactivate the shared one and take other apps' mail
  offline. It only adds its own rule (`bids-intake`) to the existing set.

## Tests

```bash
python3 -m pytest tests/unit/ -q
```

Tests mock AWS and e2open; no network or credentials required. The email fixture
in `tests/fixtures/` is synthetic — no real load or third-party data.
