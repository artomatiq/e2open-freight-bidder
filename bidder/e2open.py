"""Client for e2open's carrier TMS portal (na-app.tms.e2open.com).

Reverse-engineered request flow (all confirmed via Postman — see CLAUDE.md):

    login()             POST /security/login.do
    fetch_offer_form()  POST /MakeAnOffer.jsp        (loadID=<tms>)
    submit_bid()        POST /CarrierConfirmOffer.jsp

The app is an older JSP app: cookie-session auth (JSESSIONID), no JSON API, no
CSRF tokens. It does basic Origin/Referer validation, so every authenticated POST
carries matching Origin/Referer headers. Uses a real requests.Session cookie jar
because JSESSIONID rotates on login and TMS_ROUTEID must be preserved throughout.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import requests

BASE_URL = "https://na-app.tms.e2open.com"

# Static hidden fields on the offer form, each suffixed with the loadID.
STATIC_FIELDS = (
    "companyID",
    "carrDefaultCurr",
    "rptCurrency",
    "offerDecrement",
    "rateCurr",
    "showReservePrice",
    "allowEqualOffer",
)

MAX_RATE = 1_000_000_000_000

# JS array literals present when multiple service/equipment combos exist.
_SERVICE_LEVEL_RE = re.compile(r"var\s+newServiceLevel\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_SERVICE_EQUIP_RE = re.compile(r"var\s+newServiceEquipment\s*=\s*(\[.*?\])\s*;", re.DOTALL)


class E2openError(Exception):
    """Any failure talking to the e2open portal (login, scrape, or submit)."""


@dataclass
class OfferForm:
    """Everything scraped from MakeAnOffer.jsp needed to build a bid submission."""
    load_id: str
    fields: dict[str, str]          # scraped {name: value}, names include the loadID suffix
    trans_mode: str
    service_level: str
    equipment: str
    reserve_price: float | None = None

    def _v(self, base: str) -> str:
        return self.fields.get(f"{base}{self.load_id}", "")

    def show_reserve_price(self) -> bool:
        return self._v("showReservePrice").strip().lower() == "true"


@dataclass
class BidResult:
    success: bool
    message: str                    # "Save Successful" or the extracted failure reason
    dry_run: bool = False
    payload: dict[str, str] = field(default_factory=dict)


class _HiddenInputParser(HTMLParser):
    """Collect <input type=hidden name=.. value=..> and note any <select> names."""
    def __init__(self) -> None:
        super().__init__()
        self.hidden: dict[str, str] = {}
        self.selects: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and (a.get("type") or "").lower() == "hidden":
            name = a.get("name")
            if name:
                self.hidden[name] = a.get("value", "")
        elif tag == "select":
            name = a.get("name")
            if name:
                self.selects.add(name)


class E2openClient:
    def __init__(self, session: requests.Session | None = None, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )

    # --- Step 1: login -----------------------------------------------------

    def login(self, user_id: str, password: str) -> None:
        # Seed cookie-consent cookies or the login hits a consent interstitial.
        for name, value in (
            ("acceptedCoreCookies", "True"),
            ("allowFunctionalCookies", "True"),
            ("allowPerfAnalyticCookies", "False"),
        ):
            self.session.cookies.set(name, value, domain="na-app.tms.e2open.com")

        url = f"{self.base_url}/security/login.do"
        resp = self.session.post(
            url,
            data={
                "loginPageAction": "login",
                "lastAction": "",
                "SAML2failsafe": "false",
                "SAML2initURL": "",
                "userID": user_id,
                "password": password,
            },
            headers=self._post_headers(referer=url),
            allow_redirects=True,
        )
        resp.raise_for_status()

        # A JSESSIONID existing is NOT proof of success — confirm by hitting an
        # authenticated page and checking we're not bounced back to the login form.
        if not self._is_authenticated():
            reason = "Invalid Username or Password" if "Invalid Username" in resp.text else "login not confirmed"
            raise E2openError(f"e2open login failed: {reason}.")

    def _is_authenticated(self) -> bool:
        url = f"{self.base_url}/CarrierLoadsToMakeOffer.jsp"
        resp = self.session.get(url, headers={"Referer": f"{self.base_url}/"})
        if resp.status_code != 200:
            return False
        text = resp.text
        # The login page renders a password field / login action; an authed page won't.
        looks_like_login = ('name="password"' in text) or ("loginPageAction" in text)
        return not looks_like_login

    # --- Step 2: fetch + scrape the offer form -----------------------------

    def fetch_offer_form(self, load_id: str) -> OfferForm:
        url = f"{self.base_url}/MakeAnOffer.jsp"
        resp = self.session.post(
            url,
            data={"loadID": load_id},
            headers=self._post_headers(referer=f"{self.base_url}/CarrierLoadsToMakeOffer.jsp"),
        )
        resp.raise_for_status()
        html = resp.text

        parser = _HiddenInputParser()
        parser.feed(html)
        hidden = parser.hidden

        fields: dict[str, str] = {}
        for base in STATIC_FIELDS:
            key = f"{base}{load_id}"
            if key not in hidden:
                raise E2openError(f"Offer form for load {load_id} missing field {key!r}.")
            fields[key] = hidden[key]
        # makeOfferIDs is not suffixed; its literal value is the loadID.
        fields["makeOfferIDs"] = hidden.get("makeOfferIDs", load_id)

        trans_mode, service_level, equipment = self._derive_service_equipment(
            load_id, hidden, parser.selects, html
        )

        reserve = self._scrape_reserve_price(load_id, hidden)

        return OfferForm(
            load_id=load_id,
            fields=fields,
            trans_mode=trans_mode,
            service_level=service_level,
            equipment=equipment,
            reserve_price=reserve,
        )

    def _derive_service_equipment(self, load_id, hidden, selects, html):
        """Return (trans_mode, service_level, equipment).

        Two shapes (CLAUDE.md Step 2B):
          - single valid combo -> rendered as plain hidden inputs, just scrape.
          - multiple combos    -> live <select> + JS arrays; derive from them.
        """
        tm_key = f"transMode{load_id}"
        sl_key = f"serviceLevel{load_id}"
        eq_key = f"equipment{load_id}"

        # Single-combo shape: hidden inputs are present directly.
        if tm_key in hidden and sl_key in hidden and eq_key in hidden:
            return hidden[tm_key], hidden[sl_key], hidden[eq_key]

        # Multi-combo shape: parse the JS array literals.
        sl_match = _SERVICE_LEVEL_RE.search(html)
        eq_match = _SERVICE_EQUIP_RE.search(html)
        if not (sl_match and eq_match):
            raise E2openError(
                f"Load {load_id}: service/equipment not found as hidden inputs "
                f"or as newServiceLevel/newServiceEquipment JS arrays."
            )
        service_levels = json.loads(sl_match.group(1))[0]   # {serviceLevelID: transMode}
        equipment_by_mode = json.loads(eq_match.group(1))[0]  # {transMode: [[code, name], ...]}

        # Pick the service level whose equipment list is non-empty.
        non_empty = {m: e for m, e in equipment_by_mode.items() if e}
        if len(non_empty) == 0:
            raise E2openError(f"Load {load_id}: no service level has available equipment.")
        if len(non_empty) > 1:
            # CLAUDE.md open item #5: no disambiguation rule defined yet. Fail loudly.
            raise E2openError(
                f"Load {load_id}: multiple service levels have equipment "
                f"({sorted(non_empty)}); no disambiguation rule defined."
            )
        trans_mode, equip_list = next(iter(non_empty.items()))
        equipment = str(equip_list[0][0])  # numeric code
        # Reverse-lookup the serviceLevel ID for this transMode.
        service_level = next(
            (sid for sid, mode in service_levels.items() if mode == trans_mode), ""
        )
        if not service_level:
            raise E2openError(f"Load {load_id}: could not map transMode {trans_mode!r} to a service level.")
        return trans_mode, service_level, equipment

    @staticmethod
    def _scrape_reserve_price(load_id, hidden) -> float | None:
        """Best-effort: capture a disclosed reserve price if one is present."""
        for name, value in hidden.items():
            low = name.lower()
            if low.startswith("reserve") and name.endswith(load_id):
                try:
                    return float(re.sub(r"[,$\s]", "", value))
                except (TypeError, ValueError):
                    return None
        return None

    # --- Step 3: submit the bid --------------------------------------------

    def build_payload(self, form: OfferForm, rate: float, expdate: str, exptime: str,
                      comments: str = "", group: str = "") -> dict[str, str]:
        lid = form.load_id
        return {
            f"showReservePrice{lid}": form._v("showReservePrice"),
            f"allowEqualOffer{lid}": form._v("allowEqualOffer"),
            f"companyID{lid}": form._v("companyID"),
            f"carrDefaultCurr{lid}": form._v("carrDefaultCurr"),
            f"rptCurrency{lid}": form._v("rptCurrency"),
            f"offerDecrement{lid}": form._v("offerDecrement"),
            f"rate{lid}": f"{rate:.2f}",
            f"rateCurr{lid}": form._v("rateCurr"),
            f"quickDate{lid}": "",
            f"expdate{lid}": expdate,
            f"exptime{lid}": exptime,
            f"group{lid}": group,
            f"comments{lid}": comments,
            "makeOfferIDs": lid,
            f"transMode{lid}": form.trans_mode,
            f"serviceLevel{lid}": form.service_level,
            f"equipment{lid}": form.equipment,
        }

    def submit_bid(self, form: OfferForm, rate: float, expdate: str, exptime: str,
                   comments: str = "", group: str = "", dry_run: bool = True) -> BidResult:
        self._validate_bid(form, rate)
        payload = self.build_payload(form, rate, expdate, exptime, comments, group)

        # Defensive check just before sending (CLAUDE.md: guard against the
        # intermittent "Invalid Currency Code" / 0.00 ??? failure).
        lid = form.load_id
        if not payload[f"rate{lid}"] or not payload[f"rateCurr{lid}"]:
            raise E2openError("Refusing to submit: rate or rateCurr is empty.")

        if dry_run:
            return BidResult(success=True, message="DRY RUN — not submitted", dry_run=True, payload=payload)

        url = f"{self.base_url}/CarrierConfirmOffer.jsp"
        resp = self.session.post(
            url, data=payload,
            headers=self._post_headers(referer=f"{self.base_url}/MakeAnOffer.jsp"),
        )
        resp.raise_for_status()
        return self._parse_result(resp.text, payload)

    def _validate_bid(self, form: OfferForm, rate: float) -> None:
        if not (0 <= rate < MAX_RATE):
            raise E2openError(f"Rate {rate} outside allowed range [0, {MAX_RATE}).")
        if form.show_reserve_price() and form.reserve_price and form.reserve_price > 0:
            if not rate < form.reserve_price:
                raise E2openError(
                    f"Rate {rate} must be strictly less than reserve price {form.reserve_price}."
                )

    @staticmethod
    def _parse_result(html: str, payload: dict[str, str]) -> BidResult:
        if "rowstatussuccess" in html:
            return BidResult(success=True, message="Save Successful", payload=payload)
        warn = re.search(r'class="rowstatuswarn"[^>]*>(.*?)</', html, re.DOTALL)
        reason = re.sub(r"\s+", " ", warn.group(1)).strip() if warn else "Unknown failure (no status span found)."
        return BidResult(success=False, message=reason, payload=payload)

    # --- helpers -----------------------------------------------------------

    def _post_headers(self, referer: str) -> dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base_url,
            "Referer": referer,
        }
