"""Unit tests for bidder.e2open — scraping/derivation logic, no network."""
import pytest

from bidder.e2open import BASE_URL, E2openClient, E2openError, LoadNotAvailableError, OfferForm

LID = "208803999"


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeSession:
    """Minimal stand-in: post() returns a preset response, records the call."""
    def __init__(self, text):
        self._text = text
        self.headers = {}
        self.calls = []

    def post(self, url, data=None, headers=None, **kw):
        self.calls.append((url, data, headers))
        return FakeResp(self._text)


def _hidden(name, value):
    return f'<input type="hidden" name="{name}" value="{value}">'


SINGLE_COMBO_HTML = "<form>" + "".join([
    _hidden(f"companyID{LID}", "COMP1"),
    _hidden(f"carrDefaultCurr{LID}", "USD"),
    _hidden(f"rptCurrency{LID}", "USD"),
    _hidden(f"offerDecrement{LID}", "0"),
    _hidden(f"rateCurr{LID}", "USD"),
    _hidden(f"showReservePrice{LID}", "false"),
    _hidden(f"allowEqualOffer{LID}", "false"),
    _hidden("makeOfferIDs", LID),
    _hidden(f"transMode{LID}", "TL"),
    _hidden(f"serviceLevel{LID}", "2464"),
    _hidden(f"equipment{LID}", "3005"),
]) + "</form>"

MULTI_COMBO_HTML = "<form>" + "".join([
    _hidden(f"companyID{LID}", "COMP1"),
    _hidden(f"carrDefaultCurr{LID}", "USD"),
    _hidden(f"rptCurrency{LID}", "USD"),
    _hidden(f"offerDecrement{LID}", "0"),
    _hidden(f"rateCurr{LID}", "USD"),
    _hidden(f"showReservePrice{LID}", "false"),
    _hidden(f"allowEqualOffer{LID}", "false"),
    _hidden("makeOfferIDs", LID),
]) + f'<select name="serviceLevel{LID}"></select>' + """
<script>
var newServiceLevel = [{"2458":"IM","2464":"TL"}];
var newServiceEquipment= [{"IM":[],"TL":[[3005,"53 DRY VAN"]]}];
</script>
</form>"""


def _client(html):
    return E2openClient(session=FakeSession(html))


# --- scraping both form shapes ---

def test_scrape_single_combo():
    form = _client(SINGLE_COMBO_HTML).fetch_offer_form(LID)
    assert form.trans_mode == "TL"
    assert form.service_level == "2464"
    assert form.equipment == "3005"
    assert form.fields[f"companyID{LID}"] == "COMP1"
    assert form.fields["makeOfferIDs"] == LID


def test_scrape_multi_combo_picks_nonempty_equipment():
    form = _client(MULTI_COMBO_HTML).fetch_offer_form(LID)
    # IM has empty equipment; TL is the only one with equipment -> chosen.
    assert form.trans_mode == "TL"
    assert form.service_level == "2464"
    assert form.equipment == "3005"


def test_missing_companyID_means_load_not_available():
    # No companyID = the load left the spot market; e2open serves the page
    # without the offer's hidden fields. Should be the clear, dedicated error.
    broken = SINGLE_COMBO_HTML.replace(_hidden(f"companyID{LID}", "COMP1"), "")
    with pytest.raises(LoadNotAvailableError) as exc:
        _client(broken).fetch_offer_form(LID)
    assert exc.value.load_id == LID


def test_scrape_missing_other_static_field_still_raises_generic():
    # A different missing field (with companyID present) is an unexpected form
    # shape, not a taken load — keep the generic diagnostic.
    broken = SINGLE_COMBO_HTML.replace(_hidden(f"rateCurr{LID}", "USD"), "")
    with pytest.raises(E2openError, match="rateCurr"):
        _client(broken).fetch_offer_form(LID)


def test_multi_combo_with_two_nonempty_is_ambiguous():
    html = MULTI_COMBO_HTML.replace('"IM":[]', '"IM":[[9,"X"]]')
    with pytest.raises(E2openError, match="disambiguation"):
        _client(html).fetch_offer_form(LID)


# --- payload construction ---

def test_build_payload_shape():
    form = _client(SINGLE_COMBO_HTML).fetch_offer_form(LID)
    payload = _client(SINGLE_COMBO_HTML).build_payload(
        form, rate=1000000.0, expdate="07/13/2026", exptime="18:00", comments="technical test"
    )
    assert payload[f"rate{LID}"] == "1000000.00"
    assert payload[f"rateCurr{LID}"] == "USD"
    assert payload[f"comments{LID}"] == "technical test"
    assert payload["makeOfferIDs"] == LID
    assert payload[f"transMode{LID}"] == "TL"
    assert payload[f"quickDate{LID}"] == ""


# --- result parsing ---

def test_parse_success():
    r = E2openClient._parse_result('<span class="rowstatussuccess">Save Successful</span>', {})
    assert r.success and r.message == "Save Successful"


def test_parse_failure_extracts_reason():
    html = '<span class="rowstatuswarn">Load is no longer available on the Spot Market.</span>'
    r = E2openClient._parse_result(html, {})
    assert not r.success
    assert "no longer available" in r.message


# --- reserve price validation ---

def test_reserve_price_blocks_too_high_bid():
    form = OfferForm(
        load_id=LID,
        fields={f"showReservePrice{LID}": "true", f"rateCurr{LID}": "USD"},
        trans_mode="TL", service_level="2464", equipment="3005",
        reserve_price=5000.0,
    )
    client = E2openClient(session=FakeSession(""))
    with pytest.raises(E2openError, match="reserve"):
        client._validate_bid(form, rate=6000.0)
    # below reserve is fine
    client._validate_bid(form, rate=4000.0)


# --- dry run submit builds payload but does not send ---

def test_dry_run_does_not_post():
    session = FakeSession(SINGLE_COMBO_HTML)
    client = E2openClient(session=session)
    form = client.fetch_offer_form(LID)
    before = len(session.calls)
    result = client.submit_bid(form, rate=1000000.0, expdate="07/13/2026", exptime="18:00",
                               comments="technical test", dry_run=True)
    assert result.dry_run and result.success
    assert result.payload[f"rate{LID}"] == "1000000.00"
    assert len(session.calls) == before  # no additional POST happened
