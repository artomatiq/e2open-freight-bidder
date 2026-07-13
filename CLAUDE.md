# e2open-freight-bidder

## What this is

Automates the mechanical part of bidding on freight spot-market loads through e2open's carrier TMS portal (`na-app.tms.e2open.com`). A human still decides whether to bid and at what rate — this project handles login, pulling the load's bid form, and submitting the offer, all within e2open's 30-minute bid window.

**Workflow**: spot market load alert email arrives → human reads it, decides whether to bid → human forwards the email to a dedicated intake inbox with the desired rate on the first line → automation logs in, scrapes the load's offer form, submits the bid → confirmation email sent back to whoever forwarded it.

## Stack preferences

- **Python** for the automation client and Lambda functions — use `requests.Session()` for the e2open client (handles the cookie jar automatically, including the session-rotation-on-login behavior noted below)
- AWS for infra: SES (email intake) → Lambda (parse + orchestrate) → Secrets Manager (credentials)
- No headless browser / Playwright / Puppeteer / Selenium needed — the whole e2open flow is plain server-rendered HTML with cookie-session auth, confirmed working via raw HTTP requests in Postman

## e2open portal — reverse-engineered request flow

E2open's app is an older JSP app: no JSON API, no CSRF tokens, session-cookie auth (`JSESSIONID`). Every request below was confirmed working end-to-end via Postman, including a real successful bid submission.

**IMPORTANT — unresolved before production use**: have not yet confirmed with e2open support whether scripted/automated submission is permitted under the carrier contract/ToS. The portal does perform basic `Origin`/`Referer` validation, meaning they've made some effort to restrict non-browser clients even though it's trivially satisfied. Confirm with e2open before this runs unattended against real loads.

### Step 0: Email trigger

Alerts arrive from `noreply@tms.blujaysolutions.net`, plain text. Extract load ID with:
```
/TMS ID (\d+)/
```
Human-forwarded emails (to a separate intake inbox, not the original alert inbox) should have the rate on the first line in a strict format, e.g. `RATE: 1234.56` — validate it parses as a positive number, and validate the sender against an allowlist, before doing anything else. Fail loudly (email back) on any parse/validation failure.

### Step 1: Login

```
POST https://na-app.tms.e2open.com/security/login.do
Content-Type: application/x-www-form-urlencoded
Origin: https://na-app.tms.e2open.com
Referer: https://na-app.tms.e2open.com/security/login.do

loginPageAction=login&lastAction=&SAML2failsafe=false&SAML2initURL=&userID=<USERNAME>&password=<PASSWORD>
```

Gotchas:
- Seed cookie-consent cookies before this request or you'll hit a consent interstitial first: `acceptedCoreCookies=True; allowFunctionalCookies=True; allowPerfAnalyticCookies=False`
- No MFA/CAPTCHA observed
- `JSESSIONID` **rotates on successful login** — use a real cookie-jar HTTP client, don't hardcode session IDs
- `TMS_ROUTEID` cookie must be preserved on every subsequent request (sticky-session load balancer routing) — dropping it can misroute you to a backend without your session
- Every authenticated POST needs matching `Origin`/`Referer` headers or you get an explicit "untrusted origin" error page
- A `JSESSIONID` existing is not proof of login success — confirm by requesting an authenticated page next and checking you're not bounced back to the login form. Failed logins land back on the login page (sometimes with `Invalid Username or Password` in a hidden field's query string).

### Step 2: Fetch the offer form

```
POST https://na-app.tms.e2open.com/MakeAnOffer.jsp
Content-Type: application/x-www-form-urlencoded
Origin: https://na-app.tms.e2open.com
Referer: https://na-app.tms.e2open.com/CarrierLoadsToMakeOffer.jsp

loadID=<TMS_ID>
```

Can be called directly with just the TMS ID — no need to go through the search/list page first.

Parse the HTML response for:

**A. Static hidden fields** (all suffixed with `{loadID}`): `companyID`, `carrDefaultCurr`, `rptCurrency`, `offerDecrement`, `rateCurr`, `showReservePrice`, `allowEqualOffer`. Plus `makeOfferIDs` (not suffixed — literal value is the loadID).

**B. `serviceLevel` / `equipment` / `transMode`** — two possible shapes depending on how many valid options exist for the load:
  - If only one valid combo exists, e2open renders them as plain hidden inputs directly: `<input type="hidden" name="transMode{loadID}" value="TL">`, `serviceLevel{loadID}`, `equipment{loadID}` — just scrape these.
  - If multiple combos exist, they instead show as a live `<select>` and the valid combos are embedded as JS array literals in the page:
    ```js
    var newServiceLevel = [{"2458":"IM","2464":"TL"}];
    var newServiceEquipment= [{"IM":[],"TL":[[3005,"53 DRY VAN"]]}];
    ```
    Regex these two arrays out of the raw HTML (valid JSON once you strip `var x = ... ;`), then pick the service level whose equipment array is non-empty. `transMode` = the string value in `newServiceLevel` for that key; `equipment` = the numeric code from the equipment array. (No case with multiple non-empty options has been seen yet — needs a disambiguation rule if it comes up.)

**C. Fields the automation supplies itself** (not present in the response): `rate{loadID}` (the bid), `expdate{loadID}` (MM/DD/YYYY), `exptime{loadID}` (HH:MM 24hr), `group{loadID}` (optional, blank ok), `comments{loadID}` (optional).

Validation worth replicating client-side before submitting (mirrors what the real form's JS checks): rate >= 0 and < 1,000,000,000,000; if `showReservePrice{loadID}=="true"` and a reserve price > 0 is disclosed, rate must be strictly less than it.

### Step 3: Submit the bid

```
POST https://na-app.tms.e2open.com/CarrierConfirmOffer.jsp
Content-Type: application/x-www-form-urlencoded
Origin: https://na-app.tms.e2open.com
Referer: https://na-app.tms.e2open.com/MakeAnOffer.jsp

showReservePrice{loadID}=true
allowEqualOffer{loadID}=false
companyID{loadID}=<scraped>
carrDefaultCurr{loadID}=<scraped>
rptCurrency{loadID}=<scraped>
offerDecrement{loadID}=<scraped>
rate{loadID}=<computed bid>
rateCurr{loadID}=<scraped>
quickDate{loadID}=            (blank)
expdate{loadID}=<computed>
exptime{loadID}=<computed>
group{loadID}=                (blank)
comments{loadID}=<free text>
makeOfferIDs={loadID}
transMode{loadID}=<derived>
serviceLevel{loadID}=<derived>
equipment{loadID}=<derived>
```

### Detecting success/failure

Response is HTML with a `<span>` whose `class` tells you the outcome:

- **Success**: `class="rowstatussuccess"`, text `"Save Successful"`. `Your Offer` cell shows the submitted rate + currency. Page also fires `window.opener.indicateOfferPlaced({loadID})` (confirmed this is the real signal the browser UI itself relies on).
- **Failure**: `class="rowstatuswarn"`. Confirmed failure strings:
  - `"Load is no longer available on the Spot Market."` → load expired/closed before submission landed. Keep steps 2→3 close together (minutes, not longer) to avoid this.
  - `"Invalid Currency Code"` with `Your Offer` showing `0.00 ???` → one or more fields didn't transmit correctly. Seen once during manual testing, didn't reproduce with identical field mapping on retry — add defensive validation that `rate{loadID}` and `rateCurr{loadID}` are non-empty and well-formed immediately before sending, just in case.

Parse for `class="rowstatussuccess"` → success; otherwise extract the `rowstatuswarn` text as the failure reason and surface it in the confirmation email back to the human.

## Open items

1. ~~Confirm with e2open support whether scripted submission is permitted under contract~~ — **CONFIRMED ALLOWED (2026-07-13).** Scripted submission is permitted under the carrier contract.
2. Build the pipeline: SES intake inbox (separate from the original alert inbox) → Lambda → parser (TMS ID + rate extraction, sender allowlist) → e2open client (login → scrape → submit) → confirmation email back.
3. Credentials in AWS Secrets Manager, never plaintext/env vars. (Note: the original e2open password was exposed during discovery/debugging and should already have been rotated — use the new one only via Secrets Manager going forward.)
4. Session lifetime not yet measured — determines whether Lambda re-authenticates per bid or holds a session across a shift.
5. No disambiguation rule yet for loads with multiple valid service-level/equipment combos (not encountered in testing so far).
