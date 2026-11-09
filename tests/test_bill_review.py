"""Bill review: the checks that catch wholesaler billing mistakes before approval."""
from collections import Counter, defaultdict

import bill_review as br


def history(code_counts=None, prices=None):
    code_hist = defaultdict(Counter)
    for oc, codes in (code_counts or {}).items():
        code_hist[oc].update(codes)
    price_hist = defaultdict(list, prices or {})
    return code_hist, price_hist


def line(**kw):
    base = {"ordercode": "PIP20", "description": "20mm conduit", "qty": "10",
            "price": "2.00", "total": "20.00", "accountcode": "311", "itemid": "123"}
    base.update(kw)
    return base


# --- parsing helpers --------------------------------------------------------

def test_money_parsing_handles_aroflo_strings():
    assert br.f("24.5300") == 24.53
    assert br.f("$1,234.50") == 1234.5
    assert br.f(None) == 0.0
    assert br.f("n/a", default=-1) == -1


def test_supplier_loyalty_noise_is_stripped():
    raw = "Industrial Isolator Grey Product: 13768 Earned: 24 Points on Name: Loyalty #1"
    assert br.strip_supplier_noise(raw) == "Industrial Isolator Grey"


def test_clean_desc_joins_lines_and_strips_noise():
    assert br.clean_desc("PIP50HD\nRigid Conduit 50mm") == "PIP50HD — Rigid Conduit 50mm"


def test_clean_code_keeps_first_line_only():
    assert br.clean_code("CCG056022-VX 1\nHazardous Area fitting") == "CCG056022-VX 1"
    assert br.clean_code(None) == ""


# --- account coding ---------------------------------------------------------

def test_freight_is_always_coded_to_freight():
    assert br.expected_code("Freight charge", "FRT", {}) == "425"


def test_smoke_alarm_with_battery_is_materials_not_tools():
    # "battery" is a tools keyword, but smoke alarms are job materials.
    assert br.expected_code("Smoke alarm 10yr battery", "SA10", {}) == "311"


def test_consistent_history_beats_tool_keywords():
    code_hist, _ = history({"BAT18": {"311": 3}})
    assert br.expected_code("18V battery", "BAT18", code_hist) == "311"


def test_no_confident_expectation_returns_none():
    assert br.expected_code("Widget", "W1", {}) is None


def test_uncoded_line_gets_an_edit():
    code_hist, price_hist = history()
    _, edits = br.review_bill({"lines": [line(accountcode="NA")]}, code_hist, price_hist)
    assert edits and edits[0]["changes"] == {"accountcode": "311"}


# --- findings ---------------------------------------------------------------

def test_price_increase_is_flagged():
    code_hist, price_hist = history(prices={"PIP20": [("2026/01/01", 1.50)]})
    findings, _ = br.review_bill({"lines": [line()]}, code_hist, price_hist)
    assert any(kind == "price_increase" for _, kind, _ in findings)


def test_rounding_noise_is_not_a_price_increase():
    code_hist, price_hist = history(prices={"PIP20": [("2026/01/01", 1.999)]})
    findings, _ = br.review_bill({"lines": [line()]}, code_hist, price_hist)
    assert not any(kind == "price_increase" for _, kind, _ in findings)


def test_line_maths_error_is_flagged():
    code_hist, price_hist = history()
    findings, _ = br.review_bill({"lines": [line(total="25.00")]}, code_hist, price_hist)
    assert any(kind == "line_maths" for _, kind, _ in findings)


def test_clean_line_raises_nothing():
    code_hist, price_hist = history({"PIP20": {"311": 2}}, {"PIP20": [("2026/01/01", 2.00)]})
    findings, edits = br.review_bill({"lines": [line()]}, code_hist, price_hist)
    assert findings == [] and edits == []


def test_unlinked_item_needs_a_code():
    code_hist, price_hist = history()
    findings, _ = br.review_bill({"lines": [line(itemid="")]}, code_hist, price_hist)
    assert any(kind == "needs_code" for _, kind, _ in findings)


# --- change detection -------------------------------------------------------

def test_fingerprint_changes_when_a_price_changes():
    a = {"status": "In Progress", "totalinc": "22.00", "lines": [line()]}
    b = {"status": "In Progress", "totalinc": "22.00", "lines": [line(price="2.10")]}
    assert br.bill_fingerprint(a) != br.bill_fingerprint(b)


def test_fingerprint_changes_when_pdf_verification_flips():
    # A bill stored during a PDF-reader outage must be re-reported once it can be read.
    bill = {"status": "In Progress", "totalinc": "22.00", "lines": [line()]}
    assert br.bill_fingerprint(bill, "unverified") != br.bill_fingerprint(bill, "verified")
