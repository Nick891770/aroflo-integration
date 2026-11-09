"""
AroFlo Bill Review — read-only pass (Stage 1).

For every "In Progress" bill, this independently re-checks what the AI Bill
Scanner entered and flags anything that looks wrong — WITHOUT touching AroFlo.
Nothing is written, nothing is approved. It just produces a review you can act
on (and, later, a Telegram summary).

It learns what "normal" looks like from your own processed-bill history:
  • Account coding  — which ordercodes you historically code to 311/488/425/406
  • Pricing         — the last price you paid for each ordercode

Then for each In Progress line it raises:
  • account_code   — freight on 311, a tool on 311, an uncoded 'NA' line, or a
                     part you normally code elsewhere
  • price_increase — supplier charged more than the last time you bought it
  • new_item       — an ordercode never seen before (needs a category + code)
  • line_maths     — qty x price doesn't equal the line total (scanner slip)
  • field_swap     — ordercode/description look swapped

NOT here yet (by design):
  • PDF vs scanner price/qty match — needs the Anthropic API key (next module)
  • Any writes / bill approval     — Stage 2, once this has earned trust

Usage:
    python bill_review.py                 # review current In Progress bills
    python bill_review.py --history 20    # learn from more history pages
    python bill_review.py --supplier sher # only bills from a supplier (substr)
"""

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime

import requests

# Windows consoles default to cp1252 and choke on the emoji in the report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from aroflo_connector import AroFloConnector
from config import AROFLO_HOST_IP, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# The claude CLI on the server (runs on a Claude subscription, no per-call API cost) extracts the
# supplier PDF independently, the same headless pattern as the research agents.
CLAUDE_BIN = "claude"
PDF_TIMEOUT = 220        # seconds per bill PDF
PRICE_MATCH_TOL = 0.02   # unit price: scanner stores 2dp, PDF often 4dp — tolerate rounding
TOTAL_MATCH_TOL = 0.01   # line total = actual dollars charged, shouldn't round — near-exact

# ---- account codes (Xero chart of accounts — adjust to yours) ----------------------------
ACCOUNT_NAMES = {
    "311": "Materials",
    "488": "Tools/Consumables",
    "425": "Freight",
    "406": "Credit Card Fees",
    "NA":  "UNCODED",
}
FREIGHT_KW = ["freight", "delivery", "shipping", "cartage", "postage", "courier"]
CARD_KW    = ["credit card", "card fee", "card surcharge", "merchant fee", "surcharge"]
# tight, high-confidence tool/consumable signals — lean on history for the rest
TOOLS_KW   = ["battery", "batteries", "driver bit", "impact bit", "power bit",
              "drill bit", "hole saw", "caulking gun", "tape measure", "hacksaw"]
# Job materials that often say "battery" — smoke alarms (Brooks, Clipsal, any
# brand, battery or mains), emergency/exit fittings. Always 311
# (every past line of these was 311, but "battery" would recode them to 488).
MATERIALS_KW = ["smoke", "heat alarm", "heat detector", "emergency", "exit sign", "exit light"]

# ---- thresholds ------------------------------------------------------------
PRICE_TOL   = 0.005   # ignore <0.5% price moves (rounding noise)
MATHS_TOL   = 0.02    # qty*price vs total tolerance, dollars
DESC_MIN    = 6       # don't rewrite a description using a PDF snippet shorter than this
DESC_MAX    = 200     # cap what we write back into the description field


def f(x, default=0.0):
    """AroFlo returns money as strings like '24.5300'. Parse safely."""
    try:
        return float(str(x).replace("$", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return default


# Wholesaler junk the scanner appends to a description. Tradezone prints its loyalty
# block on its own line of the invoice, but it arrives concatenated onto the item text
# ("...Industrial Isolator Grey Product: 13768 Earned: 24 Points on Name: Loyalty #1")
# and rides through to inventory and then the client's invoice. Each pattern is anchored
# on a token combination that can't plausibly be part of a real part description.
NOISE_PATTERNS = [
    r"\s*Product:\s*\d+\s+Earned:.*$",
    r"\s*Earned:\s*\d+\s+Points\b.*$",
    r"\s*\d+\s+Total\s+Points\s+Earned\b.*$",
    r"\s*Name:\s*Loyalty\s*#.*$",
    r"\s*Loyalty\s*#\s*\d+.*$",
]


def strip_supplier_noise(s):
    """Cut a supplier's loyalty/points block off the end of a description.
    Truncates at the EARLIEST match so nothing after the junk survives."""
    text = str(s or "")
    cut = len(text)
    for pat in NOISE_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            cut = min(cut, m.start())
    return text[:cut].rstrip(" -–—,;:")


def clean_desc(desc):
    """Line descriptions often arrive as 'PIP50HD\\nRigid Conduit ...'.
    Return a single readable line, with supplier loyalty noise removed."""
    if not desc:
        return ""
    parts = [p.strip() for p in str(desc).splitlines() if p.strip()]
    joined = " — ".join(parts) if parts else ""
    return strip_supplier_noise(joined).strip()


def clean_code(oc):
    """Order codes can also carry a trailing newline + description text
    ('CCG056022-VX 1\\nHazardous Area...'). Keep just the code on the first line."""
    if not oc:
        return ""
    return str(oc).splitlines()[0].strip()


def fetch_bills(conn, status, max_pages=1, join="lineitems"):
    """Pull bills of a given status with line items joined. Returns a list.
    Client-side filters on status too — AroFlo's status where-clause is loose
    (bogus values still return the In Progress set)."""
    out = []
    for pg in range(1, max_pages + 1):
        resp = conn.request("bills", {"where": f"and|status|=|{status}",
                                      "join": join, "page": str(pg)})
        bills = resp.get("zoneresponse", {}).get("bills", []) if isinstance(resp, dict) else []
        if not bills:
            break
        # keep only the status we asked for
        out.extend(b for b in bills if str(b.get("status", "")).strip().lower() == status.lower())
        if len(bills) < 25:          # last page (AroFlo pages ~25)
            break
    return out


def build_history(processed):
    """From processed bills, learn per-ordercode coding + last price."""
    code_hist  = defaultdict(Counter)            # ordercode -> Counter(accountcode)
    price_hist = defaultdict(list)               # ordercode -> [(date, price)]
    lines = 0
    for b in processed:
        date = str(b.get("dateinvoiced", ""))
        for ln in b.get("lines", []):
            oc = clean_code(ln.get("ordercode")).upper()
            if not oc:
                continue
            lines += 1
            ac = (ln.get("accountcode") or "").strip()
            if ac:
                code_hist[oc][ac] += 1
            price_hist[oc].append((date, f(ln.get("price"))))
    # sort each part's price history by invoice date
    for oc in price_hist:
        price_hist[oc].sort(key=lambda t: t[0])
    return code_hist, price_hist, lines


def expected_code(desc, ordercode, code_hist):
    """Best guess at the correct account code for a line."""
    d = (desc or "").lower()
    if any(k in d for k in FREIGHT_KW):
        return "425"
    if any(k in d for k in CARD_KW):
        return "406"
    if any(k in d for k in MATERIALS_KW):
        return "311"
    # strong history: bought >=2x and always coded the same way. Beats the tools
    # keywords — the business's own consistent coding is better evidence than a word
    # (code audit B3, 16 Oct 2026).
    oc = (ordercode or "").strip().upper()
    hist = code_hist.get(oc)
    if hist and sum(hist.values()) >= 2 and len(hist) == 1:
        return next(iter(hist))
    if any(k in d for k in TOOLS_KW):
        return "488"
    return None          # no confident expectation -> don't flag


def review_bill(bill, code_hist, price_hist):
    """Return (findings, edits) for one bill. findings = (sev, type, msg) for things
    to flag; edits = auto-fixable corrections {line, changes, desc}."""
    findings, edits = [], []
    for i, ln in enumerate(bill.get("lines", []), 1):
        oc    = clean_code(ln.get("ordercode"))
        ocU   = oc.upper()
        desc  = clean_desc(ln.get("description"))
        qty   = f(ln.get("qty"))
        price = f(ln.get("price"))
        total = f(ln.get("total"))
        actual_ac = (ln.get("accountcode") or "").strip()
        itemid = (ln.get("itemid") or "").strip()
        tag = f"L{i} {desc[:46] or oc}"

        # --- account code → auto-fixable edit ---
        exp = expected_code(desc, oc, code_hist)
        want = None
        if actual_ac == "NA":
            want = exp or "311"
        elif exp and exp != actual_ac:
            want = exp
        if want:
            edits.append({"line": ln, "changes": {"accountcode": want},
                          "desc": f"{tag}: account code {actual_ac or '—'}→{want} {ACCOUNT_NAMES.get(want,'')}"})

        # --- price increase vs last time we bought this part ---
        if ocU and ocU in price_hist and price > 0:
            last_date, last = price_hist[ocU][-1]
            if last > 0 and price > last * (1 + PRICE_TOL):
                pct = (price / last - 1) * 100
                findings.append(("flag", "price_increase",
                    f"{tag}: ${price:.2f} vs ${last:.2f} last paid ({last_date}) — up {pct:.0f}%"))

        # --- not linked to inventory → needs a category + generic code ---
        if oc and not itemid:
            seen = " (bought before, still uncatalogued)" if ocU in code_hist else ""
            findings.append(("info", "needs_code",
                f"{tag}: '{oc}' not linked to inventory{seen}"))

        # --- line arithmetic: rounding-aware (unit price shown to 2dp, stored to 4) ---
        maths_tol = max(MATHS_TOL, 0.01 * qty)
        if qty and price and abs(qty * price - total) > maths_tol:
            findings.append(("flag", "line_maths",
                f"{tag}: {qty:g} x ${price:.2f} = ${qty*price:.2f}, but line total is ${total:.2f}"))

        # --- field swap heuristic ---
        if oc and " " in oc and not any(c.isdigit() for c in oc):
            findings.append(("info", "field_swap",
                f"{tag}: ordercode '{oc}' looks like a description — possible field swap"))

    return findings, edits


# ----------------------------------------------------------------------------
# PDF pass — verify the scanner's lines against the source invoice (claude CLI)
# ----------------------------------------------------------------------------
def fetch_documents(conn, billid):
    """Documents attached to one bill (join=documentsandphotos)."""
    resp = conn.request("bills", {"where": f"and|billid|=|{billid}",
                                  "join": "documentsandphotos", "page": "1"})
    bills = resp.get("zoneresponse", {}).get("bills", []) if isinstance(resp, dict) else []
    if not bills:
        return []
    docs = bills[0].get("documentsandphotos", [])
    return docs if isinstance(docs, list) else [docs]


def find_pdf_doc(docs):
    """Pick the attached supplier PDF (the scanner's source), if any."""
    for d in docs:
        if (d.get("name") or "").lower().endswith(".pdf") and d.get("url"):
            return d
    return next((d for d in docs if d.get("url")), None)


def download_pdf(url, dest):
    """Download the signed AroFlo doc URL (no extra auth) to dest. The url is
    time-limited (expires=...), so this must run during the same pass."""
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200 and r.content[:5] == b"%PDF-":
            with open(dest, "wb") as fh:
                fh.write(r.content)
            return True
    except requests.RequestException:
        pass
    return False


def _extract_json(out):
    """Pull a JSON object out of the CLI's text output (tolerate ``` fences)."""
    out = (out or "").strip()
    out = re.sub(r"^```(?:json)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        pass
    s, e = out.find("{"), out.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(out[s:e + 1])
        except (ValueError, TypeError):
            return None
    return None


# Why the last CLI read failed. The blind-run alert says "the reader is down";
# this is what lets it say *why* instead of making someone go and find out.
#
# ⚠️ The CLI prints authentication failures to STDOUT, not stderr. On 26 Aug 2026
# the OAuth refresh token expired and the alerts showed only stderr's routine
# "no stdin data received" warning — two services read as a stdin bug for two
# days. Both streams get looked at here, and a lapsed login is named as one.
LAST_CLI_ERROR = None

_CLI_NOISE = re.compile(r"^Warning: no stdin data received in \d+s.*$", re.M)
_CLI_AUTH = re.compile(
    r"OAuth session expired|Failed to authenticate|Invalid API key|Please run .?/login", re.I)


def _describe_cli_failure(proc=None, exc=None):
    """One line naming the real cause of a failed `claude` run."""
    if exc is not None:
        if isinstance(exc, subprocess.TimeoutExpired):
            return f"timed out after {PDF_TIMEOUT}s"
        return f"{type(exc).__name__}: {exc}"

    parts = []
    for stream in (proc.stdout, proc.stderr):
        cleaned = _CLI_NOISE.sub("", stream or "").strip()
        if cleaned:
            parts.append(cleaned)
    combined = "\n".join(parts)

    if _CLI_AUTH.search(combined):
        return ("the claude login on this box has lapsed — "
                + combined.splitlines()[0]
                + " (fix: ssh to the server, run `claude`, then `/login`)")
    return (combined or f"exit {proc.returncode}, no output").strip()[:300]




# The CLI enforces this shape itself (--json-schema, 25 Oct 2026), so a read can't come
# back as prose or a fenced/half-closed object. Numbers allow null to match the prompt's
# "use null for any value not present"; the reconcile check still judges the content.
_NUM = {"type": ["number", "null"]}
_STR = {"type": ["string", "null"]}
INVOICE_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "supplier": _STR,
        "invoice_number": _STR,
        "is_credit_note": {"type": "boolean"},
        "lines": {"type": "array", "items": {
            "type": "object",
            "properties": {"part_number": _STR, "description": _STR, "qty": _NUM,
                           "unit_price": _NUM, "line_total": _NUM},
            "required": ["part_number", "description", "qty", "unit_price", "line_total"],
            "additionalProperties": False}},
        "subtotal_ex_gst": _NUM,
        "gst": _NUM,
        "total_inc_gst": _NUM,
    },
    "required": ["supplier", "invoice_number", "is_credit_note", "lines",
                 "subtotal_ex_gst", "gst", "total_inc_gst"],
    "additionalProperties": False,
})


def _structured_result(stdout):
    """The CLI's --output-format json envelope → the schema-checked object, or None.
    Falls back to scraping JSON out of the text result if structured_output is absent."""
    try:
        env = json.loads(stdout)
    except (ValueError, TypeError):
        return _extract_json(stdout)
    if not isinstance(env, dict) or env.get("is_error"):
        return None
    data = env.get("structured_output")
    if isinstance(data, dict):
        return data
    return _extract_json(env.get("result"))


def extract_pdf_lines(pdf_path, model="sonnet"):
    """Headless claude CLI (Max, $0) reads the PDF → structured line items + the
    invoice's printed totals (so the extraction can be self-checked against them).
    Reads the PDF COLD — it is never shown AroFlo's values, so the comparison in
    compare_pdf stays an independent cross-check."""
    global LAST_CLI_ERROR
    prompt = (
        f"Read the file {os.path.basename(pdf_path)} — an Australian electrical "
        "wholesaler invoice (e.g. Haymans, Tradezone, Rexel, MMEM, Middys). Extract it "
        "to JSON. Output ONLY JSON (no prose, no code fence):\n"
        '{"supplier": string, "invoice_number": string, "is_credit_note": boolean, '
        '"lines": [{"part_number": string, "description": string, "qty": number, '
        '"unit_price": number, "line_total": number}], '
        '"subtotal_ex_gst": number, "gst": number, "total_inc_gst": number}\n'
        "Rules:\n"
        "- All numbers raw: no $, no commas. Use null for any value not present.\n"
        "- unit_price and line_total are EX-GST.\n"
        "- unit_price is the NETT price actually charged (after any discount) for ONE of "
        "the unit shown in the qty column, so that qty x unit_price = line_total.\n"
        "- CRITICAL, the 'Per' column: some wholesalers (Haymans) print a 'Unit Price' "
        "alongside a 'Per' divisor. Example row: 'CBL6SDIBK  34  MTR  216.6759  100  73.67' "
        "= 34 metres at $216.6759 per 100m = $73.67, so unit_price is 2.1668, NOT 216.6759. "
        "Whenever a Per column (or 'per 100', 'per C', per roll/pack/box) is present and is "
        "not 1, unit_price MUST be the printed price DIVIDED by that figure. Verify every "
        "line with qty x unit_price = line_total and correct unit_price if it doesn't hold.\n"
        "- Use the quantity INVOICED/SUPPLIED, never the quantity ordered or backordered. "
        "Haymans prints 'Ord', 'B/Ord' and 'Ship' — use Ship.\n"
        "- Include EVERY charge line printed on the invoice: parts, freight/delivery, "
        "surcharges, and a rounding line ONLY if one is literally printed. Do NOT include "
        "the subtotal/GST/total summary rows as line items.\n"
        "- Do NOT invent or back-calculate a rounding/balancing line to force the lines to "
        "add up to the total. If your extracted lines don't match the printed subtotal, "
        "leave them as-is — a mismatch means you mis-read a line, it is NOT to be papered over.\n"
        "- subtotal_ex_gst, gst and total_inc_gst are the invoice's own printed totals.\n"
        "- Read ALL pages of the document."
    )
    try:
        proc = subprocess.run(
            # No --allowedTools / --dangerously-skip-permissions (22 Sep 2026 audit):
            # in -p mode a Read inside cwd (the temp dir holding this one PDF) needs
            # no approval, and every other tool call is auto-denied — so a crafted
            # supplier PDF can't turn into a shell command or a read of ~/.env.
            [CLAUDE_BIN, "-p", prompt,
             "--model", model, "--output-format", "json", "--json-schema", INVOICE_SCHEMA],
            cwd=os.path.dirname(pdf_path) or ".", capture_output=True, text=True,
            timeout=PDF_TIMEOUT, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            LAST_CLI_ERROR = _describe_cli_failure(proc=proc)
            return None
        data = _structured_result(proc.stdout)
        if data is None:
            # exit 0 but no usable object (is_error envelope, or unparseable) — say why
            LAST_CLI_ERROR = _describe_cli_failure(proc=proc)
        return data
    except (subprocess.TimeoutExpired, OSError) as exc:
        LAST_CLI_ERROR = _describe_cli_failure(exc=exc)
        return None


# Attempt ladder: a quick sonnet pass, a retry, then escalate to opus for the
# invoice that won't parse or won't reconcile against its own printed subtotal.
EXTRACT_MODELS = ["sonnet", "sonnet", "opus"]
RECONCILE_TOL = 0.10     # $; or 1% of subtotal, whichever is larger


def reconcile_extraction(pdf_data):
    """Does the sum of the extracted line totals match the invoice's PRINTED
    subtotal? Returns (ok, diff): ok=True matches, ok=False doesn't (the reader
    likely missed/mis-read a line), ok=None when the PDF has no subtotal to check.
    diff = extracted line sum − printed subtotal."""
    if not pdf_data:
        return None, None
    sub = _fnum(pdf_data.get("subtotal_ex_gst"))
    if sub is None or sub == 0:
        return None, None
    line_sum = sum((_fnum(l.get("line_total")) or 0.0)
                   for l in (pdf_data.get("lines") or []))
    diff = line_sum - sub
    tol = max(RECONCILE_TOL, 0.01 * abs(sub))
    return abs(diff) <= tol, diff


def _persist_extraction(log_dir, billnumber, data):
    """Drop the raw extraction to disk for an audit trail (what the reader saw)."""
    if not log_dir:
        return
    try:
        os.makedirs(log_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(billnumber or "x"))
        with open(os.path.join(log_dir, f"{safe}.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


def read_invoice_pdf(pdf_path, billnumber="", log_dir=None):
    """Extract + self-verify a supplier invoice. Tries sonnet, retries, then escalates
    to opus if the output won't parse OR the line sum doesn't reconcile against the
    invoice's printed subtotal (a sign the reader missed a line). Returns the chosen
    extraction dict (annotated with _model/_attempt/_reconcile_ok/_reconcile_diff), or
    None if every attempt failed. Credit notes are accepted on the first read."""
    last = None
    for attempt, model in enumerate(EXTRACT_MODELS, 1):
        data = extract_pdf_lines(pdf_path, model=model)
        if data is None:
            continue
        last = data
        if data.get("is_credit_note"):
            data["_model"], data["_attempt"] = model, attempt
            data["_reconcile_ok"], data["_reconcile_diff"] = None, None
            break
        ok, diff = reconcile_extraction(data)
        data["_model"], data["_attempt"] = model, attempt
        data["_reconcile_ok"], data["_reconcile_diff"] = ok, diff
        if ok is not False:          # True (adds up) or None (no subtotal) → accept
            break
        # ok is False → reader missed something; fall through to the next, stronger attempt
    if last is not None:
        _persist_extraction(log_dir, billnumber, last)
    return last


def verify_pdf_identity(bill, pdf_data):
    """Soft check that the attached PDF actually belongs to this bill — compares the
    invoice number the reader found on the PDF to the bill's number. Flags only a
    clear, high-confidence mismatch (both numbers present, no digit overlap)."""
    def digits(s):
        return re.sub(r"\D", "", str(s or ""))
    dp, db = digits(pdf_data.get("invoice_number")), digits(bill.get("billnumber"))
    if len(dp) >= 4 and len(db) >= 4 and dp != db and dp not in db and db not in dp:
        return [("flag", "pdf_mismatch",
                 f"attached PDF is invoice #{pdf_data.get('invoice_number')} but bill is "
                 f"#{bill.get('billnumber')} — wrong PDF attached? verify before trusting checks")]
    return []


def _norm_pn(s):
    return (s or "").strip().upper().replace(" ", "")


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _desc_tokens(s):
    return set(re.findall(r"[A-Z0-9]+", (s or "").upper()))


def _desc_match(a, b):
    """Loose description match — the same item described slightly differently."""
    ta, tb = _desc_tokens(a), _desc_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap >= 2 and overlap / min(len(ta), len(tb)) >= 0.6


def _desc_key(s):
    """Normalise a description for 'are these the same text?' comparisons."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def noise_fix_for(line):
    """Supplier loyalty/points junk on this line's description → the cleaned text,
    or None if there's nothing to strip.

    Unlike a description *substitution* this is safe on any matched line, including
    inventory-linked ones: we only ever DELETE a recognised block, never invent text.
    That matters — on real bills the junk sits on lines that DO have an itemid, which is
    how it reaches the inventory item and then the client's invoice."""
    raw = str(line.get("description") or "")
    if not raw.strip():
        return None
    cleaned = clean_desc(raw)
    if not cleaned or len(cleaned) < DESC_MIN:
        return None                      # never strip a line down to nothing
    # compare against the same newline-flattening minus the noise removal, so we only
    # fire when actual junk was removed (not merely because newlines were collapsed)
    flat = " — ".join(p.strip() for p in raw.splitlines() if p.strip()).strip()
    if _desc_key(cleaned) == _desc_key(flat):
        return None
    return cleaned[:DESC_MAX]


def desc_fix_for(line, pdf_desc, matched_by):
    """Should we rewrite this bill line's description to the PDF's wording?
    Returns the new description, or None to leave it alone.

    Deliberately narrow — a description edit is only safe when we're certain we're
    looking at the same line, and only when nothing better already owns the wording:

      * part-number match ONLY. A description match is already close by definition, and
        an amount-only match is how we recognise a relabelled charge (invoice "Freight"
        imported as "Rounding Adjustment") — rewriting on that evidence is guesswork.
      * never on a line linked to inventory (itemid set). There the description comes
        from the catalogue item, which is the naming we WANT — overwriting it with the
        wholesaler's text is the exact problem this whole exercise is trying to remove.
      * never blank or near-blank, and not for cosmetic-only differences.
    """
    if matched_by != "code":
        return None
    if line.get("itemid"):
        return None
    new = clean_desc(pdf_desc).strip()[:DESC_MAX]
    if len(new) < DESC_MIN:
        return None
    old = clean_desc(line.get("description"))
    ko, kn = _desc_key(old), _desc_key(new)
    if not kn or ko == kn:
        return None
    # the scanner often stores 'CODE — Description' while the PDF line is just
    # 'Description' (or vice versa). Same information, so leave it be.
    if ko and (kn in ko or ko in kn):
        return None
    return new


def _ocr_key(s):
    """Part number with the scanner's usual OCR slips folded (I↔1, O↔0) — so
    'CL1900/16/16WE' and 'CLI900/16/16WE' compare equal."""
    return _norm_pn(s).translate(str.maketrans("IO", "10"))


def _code_in(oc, pn, ocr=False):
    """Does part number `pn` appear as a whole whitespace-separated token in `oc`?"""
    key = _ocr_key if ocr else _norm_pn
    want = key(pn)
    return bool(want) and any(key(t) == want for t in str(oc or "").split())


def code_fix_for(line, pdf_pn, matched_by):
    """Should we rewrite this bill line's part number to the invoice's?
    Returns the clean part number, or None to leave it alone.

    The scanner regularly stuffs the whole row into ordercode — '1 HANTRIPROOF12 LED
    W/P BATTEN…' (line no. + code + description) or 'META316053950\\nSHIFT LEVER' —
    and sometimes misreads I as 1 ('CL1900/16/16WE'). We only ever replace the field
    with the invoice's part number when that part number is ALREADY sitting in it as
    a token (OCR-folded), so the edit is a clean-up, never a guess:

      * the line must have matched its PDF line by code or description — never by
        amount alone (that's how relabelled charges match; the code tells us nothing)
      * the PDF part number must be at least 4 characters
    """
    if matched_by not in ("code", "desc"):
        return None
    pn = str(pdf_pn or "").strip()
    if len(_norm_pn(pn)) < 4:
        return None
    raw = str(line.get("ordercode") or "")
    if raw.strip() == pn:
        return None
    if not _code_in(raw, pn, ocr=True):
        return None
    return pn


def strip_code_prefix(desc, pn):
    """Description starting with the part number ('META316055210 10.00\\nMETABO CARBON
    BRUSH…') → the description without it, or None if it doesn't start that way.
    A first line that is only the code plus a bare number (the qty) is dropped whole."""
    lines = [p.strip() for p in str(desc or "").splitlines() if p.strip()]
    if not lines:
        return None
    toks = lines[0].split()
    if not toks or _ocr_key(toks[0]) != _ocr_key(pn):
        return None
    rest = toks[1:]
    if rest and not all(re.fullmatch(r"[\d.,]+", t) for t in rest):
        lines[0] = " ".join(rest)
    else:
        lines = lines[1:]
    new = clean_desc("\n".join(lines))[:DESC_MAX]
    return new if len(new) >= DESC_MIN else None


def compare_pdf(bill, pdf_data, fix_desc=False, unit_price_fix=False):
    """Scanner's bill lines vs the PDF's lines → (findings, edits). Match by part
    number, then description. Price/qty disagreements become auto-fix edits (the PDF
    is the source of truth); a line on the PDF but not the bill stays a manual flag.
    With fix_desc, a garbled description on a part-number-matched line is corrected
    to the invoice's wording too (see desc_fix_for for the guards)."""
    if not pdf_data:
        return [("info", "pdf_note", "PDF couldn't be read — not verified")], []
    if pdf_data.get("is_credit_note"):
        return [("info", "pdf_note", "credit note — handle manually")], []

    pdf_lines = list(pdf_data.get("lines") or [])
    used = [False] * len(pdf_lines)
    findings, edits = [], []
    # only derive a unit price from the invoice total when the reader's own extraction
    # reconciled against the printed subtotal — otherwise the total isn't trustworthy
    reconciled = pdf_data.get("_reconcile_ok") is not False

    for i, ln in enumerate(bill.get("lines", []), 1):
        oc = clean_code(ln.get("ordercode"))
        pn = _norm_pn(oc)
        bdesc = clean_desc(ln.get("description"))
        tag = f"L{i} {bdesc[:40] or oc}"

        idx = None
        matched_by = None
        if pn:
            idx = next((j for j, pl in enumerate(pdf_lines)
                        if not used[j] and _norm_pn(pl.get("part_number")) == pn), None)
            if idx is not None:
                matched_by = "code"
        if idx is None:
            # scanner stuffed the row into ordercode ('1 HANTRIPROOF12 LED W/P…') —
            # the invoice's part number is still in there as a whole token
            idx = next((j for j, pl in enumerate(pdf_lines)
                        if not used[j] and len(_norm_pn(pl.get("part_number"))) >= 4
                        and _code_in(ln.get("ordercode"), pl.get("part_number"))), None)
            if idx is not None:
                matched_by = "code"
        if idx is None:
            idx = next((j for j, pl in enumerate(pdf_lines)
                        if not used[j] and _desc_match(bdesc, pl.get("description"))), None)
            if idx is not None:
                matched_by = "desc"
        if idx is None:
            # last resort: match by line amount — catches the scanner relabelling a charge
            # (e.g. an invoice "Freight" imported as "Rounding Adjustment") so we neither
            # false-flag it nor duplicate it
            s_tot = f(ln.get("total"))
            if s_tot:
                idx = next((j for j, pl in enumerate(pdf_lines)
                            if not used[j] and _fnum(pl.get("line_total")) is not None
                            and abs(_fnum(pl.get("line_total")) - s_tot) <= 0.02), None)
                if idx is not None:
                    matched_by = "amount"
        if idx is None:
            continue                     # couldn't match this bill line — skip rather than cry wolf
        used[idx] = True
        pl = pdf_lines[idx]
        s_price, s_qty, s_total = f(ln.get("price")), f(ln.get("qty")), f(ln.get("total"))
        p_price, p_qty, p_total = _fnum(pl.get("unit_price")), _fnum(pl.get("qty")), _fnum(pl.get("line_total"))
        price_off = p_price is not None and abs(s_price - p_price) > PRICE_MATCH_TOL
        qty_off   = p_qty is not None and abs(s_qty - p_qty) > 0.001
        # The reader's own line must multiply out before we copy its price/qty into
        # AroFlo. A missed 'Per 100' divisor gives qty 34 x $216.68 next to a $73.67
        # total — the subtotal self-check can't see it (the totals still add up), and
        # auto-applying it writes a 100x unit price onto the job (code audit B1).
        line_consistent = not (p_price is not None and p_qty and p_total is not None
                               and abs(p_price * p_qty - p_total) > max(0.02, 0.01 * abs(p_total)))
        hold_why = "" if line_consistent else \
            " — held: PDF line doesn't multiply out (qty × unit ≠ total), check the invoice"
        if price_off:
            edits.append({"line": ln, "changes": {"price": f"{p_price}"},
                          "desc": f"{tag}: unit price ${s_price:.2f}→${p_price:.2f} (match PDF)",
                          "hold": hold_why})
        if qty_off:
            edits.append({"line": ln, "changes": {"qty": f"{p_qty}"},
                          "desc": f"{tag}: qty {s_qty:g}→{p_qty:g} (match PDF)",
                          "hold": hold_why})
        if fix_desc:
            work = ln
            # 0. part number the scanner garbled → the invoice's (see code_fix_for guards),
            #    and drop that code off the front of the description while we're there
            if (new_code := code_fix_for(ln, pl.get("part_number"), matched_by)):
                ch = {"ordercode": new_code}
                if (stripped := strip_code_prefix(ln.get("description"), new_code)):
                    ch["description"] = stripped
                old_code = " ".join(str(ln.get("ordercode") or "").split())
                edits.append({"line": ln, "changes": ch,
                              "desc": f"{tag}: part # \"{old_code[:30]}\" → {new_code}"
                                      + (" (+ code off description)" if "description" in ch else "")})
                work = dict(ln, **ch)        # later description checks see the cleaned line
            # 1. strip supplier loyalty/points junk (safe on any line, incl. inventory-linked)
            cleaned = noise_fix_for(work)
            if cleaned:
                edits.append({"line": ln, "changes": {"description": cleaned},
                              "desc": f"{tag}: strip supplier noise → \"{cleaned[:60]}\""})
            # 2. otherwise, correct a genuinely garbled description to the invoice's wording
            elif (new_desc := desc_fix_for(work, pl.get("description"), matched_by)):
                edits.append({"line": ln, "changes": {"description": new_desc},
                              "desc": f"{tag}: description → \"{new_desc[:60]}\" (match PDF)"})
        # Price and qty both agree with the invoice, yet the totals don't. That's the
        # per-pack/per-100m trap: the invoice quotes the unit price per ROLL (e.g. cable
        # at $100/100m) while the qty is in metres, so 10m prints as qty 10 @ $100 =
        # $10 — and the scanner multiplies it out to $1000. The line TOTAL is what you're
        # actually charged, so the true unit price is total ÷ qty.
        if (not price_off and not qty_off and p_total is not None
                and abs(s_total - p_total) > TOTAL_MATCH_TOL):
            derived = (p_total / p_qty) if p_qty else None
            pdf_self_inconsistent = (p_price is not None and p_qty
                                     and abs(p_price * p_qty - p_total) > max(0.02, 0.01 * abs(p_total)))
            if (unit_price_fix and derived is not None and pdf_self_inconsistent
                    and reconciled and abs(derived - s_price) > PRICE_MATCH_TOL):
                edits.append({"line": ln, "changes": {"price": f"{derived:.4f}"},
                              "desc": f"{tag}: unit price ${s_price:.2f}→${derived:.4f} "
                                      f"(per-unit from invoice total ${p_total:.2f} ÷ {p_qty:g})"})
            else:
                why = " — looks like per-pack pricing (unit price quoted per roll/pack)" \
                      if pdf_self_inconsistent else ""
                findings.append(("flag", "pdf_mismatch",
                    f"{tag}: line total scanner ${s_total:.2f} vs PDF ${p_total:.2f} (check){why}"))

    # PDF lines that matched nothing (not by part #, description, OR amount) — a line the
    # scanner may genuinely have missed. We FLAG it rather than auto-insert: adding a
    # financial line is too risky when the scanner relabels charges (a "missing" line is
    # often already on the bill under a different description). Added by hand if needed.
    for j, pl in enumerate(pdf_lines):
        if used[j]:
            continue
        d = (pl.get("description") or pl.get("part_number") or "").strip()[:40]
        if d:
            findings.append(("flag", "pdf_mismatch", f"on PDF, not on bill: {d} — add manually if needed"))

    # Aggregate safety net: even when per-line matching falls through, the bill's
    # grand total should equal the invoice's printed total. A gap here catches a
    # missing line or a mispriced one that slipped past the line-by-line pass.
    p_total = _fnum(pdf_data.get("total_inc_gst"))
    b_total = f(bill.get("totalinc"))
    if p_total is not None and b_total and abs(b_total - p_total) > max(0.10, 0.001 * p_total):
        findings.append(("flag", "pdf_mismatch",
            f"bill total ${b_total:.2f} ≠ invoice total ${p_total:.2f} "
            f"(${b_total - p_total:+.2f}) — a line is missing, mispriced, or extra"))
    return findings, edits


ADJ_KW = ("rounding", "adjustment")


def fake_adjustment(bill, pdf_data):
    """A "Rounding Adjustment" over $1 that matches no PDF line amount is the scanner
    balancing a bill it mis-read — not a real charge (a real one, e.g. freight relabelled,
    matches a PDF line by amount). Fixing the other lines then leaves the fake one behind
    and wrecks the total (seen in practice: a $477 bill dropped to $159)."""
    pdf_amounts = [_fnum(pl.get("line_total")) for pl in pdf_data.get("lines") or []]
    for ln in bill.get("lines", []):
        amt = f(ln.get("total"))
        if (abs(amt) > 1 and any(k in str(ln.get("description") or "").lower() for k in ADJ_KW)
                and not any(p is not None and abs(p - amt) <= 0.02 for p in pdf_amounts)):
            return f"scanner added a balancing '{clean_desc(ln.get('description'))}' line (${amt:.2f}) — fix by hand"
    return ""


def edits_worsen_total(bill, pdf_data, edits):
    """Refuse a set of edits that moves the bill's line sum FURTHER from the PDF's.
    Each fix can look right line by line while the bill as a whole gets worse."""
    pdf_sum = sum(_fnum(pl.get("line_total")) or 0 for pl in pdf_data.get("lines") or [])
    changes = {}
    for e in edits:
        if not e.get("insert"):
            changes.setdefault(id(e["line"]), {}).update(e["changes"])
    if not pdf_sum or not any("price" in c or "qty" in c for c in changes.values()):
        return ""
    before = after = 0.0
    for ln in bill.get("lines", []):
        tot = f(ln.get("total"))
        before += tot
        c = changes.get(id(ln), {})
        if "price" in c or "qty" in c:
            tot = f(c.get("qty", ln.get("qty"))) * f(c.get("price", ln.get("price")))
        after += tot
    if abs(after - pdf_sum) > abs(before - pdf_sum) + 0.05:
        return (f"fixes would move the bill lines from ${before:.2f} to ${after:.2f}, "
                f"further from the invoice's ${pdf_sum:.2f}")
    return ""


def pdf_check(conn, bill, tmpdir, log_dir=None, fix_desc=False, unit_price_fix=False):
    """Download + extract + compare one bill's PDF → (findings, edits). Quiet if no PDF."""
    doc = find_pdf_doc(fetch_documents(conn, bill.get("billid")))
    if not doc:
        return [], []                    # manual bill, nothing to verify against
    # supplier bill numbers can contain '/' etc. — sanitise before using as a filename
    safe_bn = re.sub(r"[^A-Za-z0-9._-]", "_", str(bill.get("billnumber", "x")))
    dest = os.path.join(tmpdir, f"bill_{safe_bn}.pdf")
    if not download_pdf(doc["url"], dest):
        return [("info", "pdf_note", "PDF wouldn't download — not verified")], []
    pdf_data = read_invoice_pdf(dest, billnumber=bill.get("billnumber"), log_dir=log_dir)
    if pdf_data is None:
        return [("info", "pdf_note", "PDF couldn't be read after retries — not verified")], []
    findings, edits = compare_pdf(bill, pdf_data, fix_desc=fix_desc,
                                  unit_price_fix=unit_price_fix)
    identity = verify_pdf_identity(bill, pdf_data)
    findings += identity
    # Never auto-apply edits from a PDF this run has itself flagged as untrustworthy:
    # the wrong invoice attached, or a reading that won't reconcile to the printed
    # subtotal. They still show, as "held — apply manually" (code audit B1).
    distrust = ("attached PDF is a different invoice" if identity
                else "PDF reader self-check failed" if pdf_data.get("_reconcile_ok") is False
                else fake_adjustment(bill, pdf_data)
                or edits_worsen_total(bill, pdf_data, edits))
    if distrust:
        for e in edits:
            e["hold"] = e.get("hold") or f" — held: {distrust}"
    if (adj := fake_adjustment(bill, pdf_data)):
        findings.append(("flag", "pdf_mismatch", adj))
    # reader self-check: the extracted lines should add up to the invoice's printed
    # subtotal. If they don't, the reader likely missed/mis-read a line — say so loudly
    # rather than trusting a comparison built on incomplete extraction.
    if pdf_data.get("_reconcile_ok") is False:
        diff = pdf_data.get("_reconcile_diff") or 0.0
        findings.append(("flag", "pdf_mismatch",
            f"PDF reader self-check failed: extracted lines are ${diff:+.2f} vs the invoice's "
            f"printed subtotal (model {pdf_data.get('_model')}, attempt {pdf_data.get('_attempt')}) "
            f"— a line may be mis-read; verify this one manually"))
    return findings, edits


def apply_line_edit(conn, billid, line, changes):
    """Update one bill line via the API, preserving every field and applying `changes`
    (e.g. {"price": "24.53"} or {"accountcode": "425"}). Returns (ok, detail).
    Validated 15 Jun 2026 (validate_write.py): preserves taskid/accountcode/qty/price.
    Rules: send all documented fields + taskid + qtyreceived=qty; OMIT empty itemid."""
    def g(k, d=""):
        return str(changes.get(k, line.get(k, d)))

    fields = {
        "billid": billid, "lineid": line.get("lineid"),
        "ordercode": g("ordercode"), "description": g("description"),
        "accountcode": g("accountcode"), "qty": g("qty", "0"),
        "qtyreceived": g("qty", "0"), "price": g("price", "0"),
        "taxcode": g("taxcode"), "taxrate": g("taxrate", "0"),
        "itemtype": g("itemtype", "Material"),
    }
    itemid = changes.get("itemid", line.get("itemid"))
    if itemid:                                  # empty itemid → 301 "itemid is invalid"
        fields["itemid"] = itemid

    def xe(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    inner = "".join(f"<{k}>{xe(v)}</{k}>" for k, v in fields.items())
    if line.get("taskid"):
        inner += f"<taskid>{xe(line['taskid'])}</taskid>"
    postxml = f"<billlineitems><billlineitem>{inner}</billlineitem></billlineitems>"
    var_string = f"zone=billlineitems&postxml={urllib.parse.quote(postxml, safe='')}"
    try:
        conn._rate_limit()
        headers = conn._generate_auth_post(var_string, accept="text/json")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = conn.session.post(conn.base_url, headers=headers, data=var_string, timeout=30)
        pr = resp.json().get("zoneresponse", {}).get("postresults", {})
        if pr.get("updatetotal", 0) >= 1 and not pr.get("errors"):
            return True, "updated"
        for u in pr.get("updates", {}).get("billlineitems", []):
            if u.get("error"):
                return False, u["error"]
        errs = pr.get("errors", [])
        return False, (errs[0].get("detail") if errs else "unknown error")
    except Exception as e:
        return False, str(e)


def apply_line_insert(conn, billid, fields):
    """Insert a NEW bill line (no lineid = insert). Used to add a line that's on the
    invoice PDF but missing from the bill, assigned to the bill's single job (taskid).
    fields: ordercode, description, qty, price, accountcode, taxcode, taxrate, itemtype, taskid.
    Returns (ok, detail). Same payload rules as the edit (omit empty itemid)."""
    f = {
        "billid": billid,
        "ordercode": str(fields.get("ordercode", "")),
        "description": str(fields.get("description", "")),
        "accountcode": str(fields.get("accountcode", "311")),
        "qty": str(fields.get("qty", "1")),
        "qtyreceived": str(fields.get("qty", "1")),
        "price": str(fields.get("price", "0")),
        "taxcode": str(fields.get("taxcode", "GST")),
        "taxrate": str(fields.get("taxrate", "10")),
        "itemtype": str(fields.get("itemtype", "Material")),
    }
    if fields.get("itemid"):
        f["itemid"] = fields["itemid"]

    def xe(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    inner = "".join(f"<{k}>{xe(v)}</{k}>" for k, v in f.items())
    if fields.get("taskid"):
        inner += f"<taskid>{xe(fields['taskid'])}</taskid>"
    postxml = f"<billlineitems><billlineitem>{inner}</billlineitem></billlineitems>"
    var_string = f"zone=billlineitems&postxml={urllib.parse.quote(postxml, safe='')}"
    try:
        conn._rate_limit()
        headers = conn._generate_auth_post(var_string, accept="text/json")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        resp = conn.session.post(conn.base_url, headers=headers, data=var_string, timeout=30)
        pr = resp.json().get("zoneresponse", {}).get("postresults", {})
        if pr.get("inserttotal", 0) >= 1 and not pr.get("errors"):
            return True, "inserted"
        for u in pr.get("inserts", {}).get("billlineitems", []):
            if u.get("error"):
                return False, u["error"]
        errs = pr.get("errors", [])
        return False, (errs[0].get("detail") if errs else "unknown error")
    except Exception as e:
        return False, str(e)


def resolve_telegram_creds():
    """Telegram token + chat id, in priority order:
      1. Aroflo/.env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
      2. a shared bot's secrets file on the server — single source of truth so
         a shared bot's token isn't duplicated (rotations only touch one place).
    """
    token, chat = TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if token and chat:
        return token, chat
    ea = os.path.expanduser(os.getenv("TELEGRAM_SECRETS_FILE", ""))
    if ea and os.path.isfile(ea):
        kv = {}
        with open(ea, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip().strip('"').strip("'")
        token = token or kv.get("TELEGRAM_BOT_TOKEN", "")
        chat = chat or kv.get("TELEGRAM_CHAT_ID", "")
    return token, chat


def send_telegram(text, dry_run=False, token=TELEGRAM_BOT_TOKEN, chat=TELEGRAM_CHAT_ID):
    """Send an HTML message via the Telegram Bot API. urllib (not curl) so an '&'
    in a part description can't silently truncate the body. Mirrors the Website
    monitor: dry-run prints, missing creds warn instead of crashing."""
    if dry_run or not token or not chat:
        print("\n--- would send to Telegram ---")
        safe = text.encode(sys.stdout.encoding or "utf-8", "replace").decode(
            sys.stdout.encoding or "utf-8", "replace")
        print(safe)
        if not dry_run and (not token or not chat):
            print("[warn] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=30).read()
        return True
    except Exception as e:
        print(f"[error] Telegram send failed: {e}", file=sys.stderr)
        return False


def build_summary(flags_flat, n_bills, clean_bills, uncoded_total, date_str,
                  n_new=0, n_changed=0, new_clean=None, notverified=0,
                  notverified_names=None, limit=6):
    """Concise, mobile-friendly HTML summary — covers only the new/changed bills."""
    def esc(s):
        return html.escape(str(s))

    notverified_names = notverified_names or []
    changed_bit = f"  ·  ♻️ {n_changed} changed" if n_changed else ""
    blocks = [f"🧾 <b>Bill Review — {esc(date_str)}</b>\n🆕 {n_new} new{changed_bit}  ·  {n_bills} in progress"]

    # NOT-verified jumps to the top: a bill the reader couldn't read wasn't checked at
    # all, so it's the one that most needs a human eye — never bury it in a footnote.
    if notverified_names:
        nv = [f"\n⛔ <b>{len(notverified_names)} NOT PDF-verified — check manually</b>"]
        nv += [f"• {esc(bn)}" for bn in notverified_names[:limit]]
        if len(notverified_names) > limit:
            nv.append(f"• …+{len(notverified_names) - limit} more")
        blocks.append("\n".join(nv))

    def section(icon, title, typ):
        items = [(bn, m) for bn, sev, t, m in flags_flat if t == typ]
        if not items:
            return None
        out = [f"\n{icon} <b>{len(items)} {title}</b>"]
        out += [f"• {esc(bn)} — {esc(m)}" for bn, m in items[:limit]]
        if len(items) > limit:
            out.append(f"• …+{len(items) - limit} more")
        return "\n".join(out)

    any_signal = bool(notverified_names)
    for blk in (section("✏️", "auto-fixed to match PDF", "edited"),
                section("⚠️", "edit FAILED (do manually)", "edit_failed"),
                section("🔒", "held — apply manually in AroFlo", "held"),
                section("✏️", "fixable (run with --apply)", "would_fix"),
                section("🔴", "on PDF, not on bill — add", "pdf_mismatch"),
                section("🚩", "price increase(s)", "price_increase"),
                section("➗", "line maths error(s)", "line_maths")):
        if blk:
            any_signal = True
            blocks.append(blk)

    if not any_signal:
        blocks.append("\n✅ Nothing to action on the new/changed bills.")
    if new_clean:
        names = ", ".join(esc(bn) for _, bn in new_clean[:limit])
        blocks.append(f"\n🆕 New &amp; clean (just check + approve): {names}")
    # extra catch-all only if there are unverified bills beyond the named (new/changed) ones
    if notverified > len(notverified_names):
        blocks.append(f"\n⚠️ {notverified} bill(s) total couldn't be PDF-verified")
    blocks.append("\n🔧 Check the edits, add any missing lines, tick <b>Update Inventory Item Pricing</b>, approve.")
    return "\n".join(blocks)


def bill_fingerprint(bill, pdf_state=""):
    """A hash of the bill's reviewable content — changes when a line's
    code/qty/price/total/accountcode or the bill total changes.

    pdf_state ("verified"/"unverified", empty on a non---pdf run) is folded in so
    that a bill whose CONTENT is unchanged but whose verification status flipped
    counts as changed. Without it, a bill stored during a PDF-reader outage stays
    'same' forever, and the findings the next working run turns up are never
    reported — which is exactly how the 24-27 Jul 2026 outage stayed silent."""
    parts = [str(bill.get("status", "")), str(bill.get("totalinc", ""))]
    for ln in bill.get("lines", []):
        parts.append("|".join(str(ln.get(k, "")) for k in
                              ("ordercode", "qty", "price", "total", "accountcode")))
    if pdf_state:
        parts.append("pdf:" + pdf_state)
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def load_seen(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_seen(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def main():
    ap = argparse.ArgumentParser(description="Read-only review of In Progress AroFlo bills")
    ap.add_argument("--history", type=int, default=15, help="processed-bill pages to learn from")
    ap.add_argument("--supplier", default="", help="filter In Progress bills by supplier substring")
    ap.add_argument("--uncoded", action="store_true", help="list every unlinked line (default: just a count)")
    ap.add_argument("--pdf", action="store_true", help="verify each bill against its source PDF (claude CLI)")
    ap.add_argument("--apply", action="store_true", help="apply the auto-fix edits (price/qty/code) to AroFlo")
    ap.add_argument("--fix-desc", action="store_true",
                    help="also correct a garbled line description to the invoice's wording, "
                         "strip supplier loyalty/points junk, and clean a part number the "
                         "scanner mashed together with the description")
    ap.add_argument("--unit-price-fix", action="store_true",
                    help="derive the true unit price from the invoice line total when the "
                         "supplier quotes per pack/per-100m (cable priced per roll)")
    ap.add_argument("--telegram", action="store_true", help="send the summary to Telegram")
    ap.add_argument("--dry-run", action="store_true", help="print the Telegram summary instead of sending")
    ap.add_argument("--all", action="store_true", help="Telegram every bill, not just new/changed ones")
    ap.add_argument("--state", default="state/seen.json", help="path to the new/changed tracking file")
    ap.add_argument("--report", default="bill_review_report.md", help="markdown report output path")
    ap.add_argument("--extract-log", default="state/extractions",
                    help="dir to save each bill's raw PDF extraction for audit (empty to disable)")
    ap.add_argument("--hold", default="state/hold.txt",
                    help="file of bill numbers/ids to NEVER auto-apply (one per line, # comments)")
    args = ap.parse_args()

    # bills whose auto-fixes you want held for manual handling (e.g. a genuine backorder)
    hold = set()
    if args.hold and os.path.exists(args.hold):
        with open(args.hold, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    hold.add(line)

    conn = AroFloConnector(host_ip=AROFLO_HOST_IP or None)

    print("Learning normal coding + pricing from processed bills...", file=sys.stderr)
    processed = fetch_bills(conn, "processed", max_pages=args.history)
    code_hist, price_hist, hist_lines = build_history(processed)
    print(f"  learned from {len(processed)} bills / {hist_lines} lines / "
          f"{len(price_hist)} distinct parts", file=sys.stderr)

    print("Fetching In Progress bills...", file=sys.stderr)
    bills = fetch_bills(conn, "in progress", max_pages=4)
    if args.supplier:
        s = args.supplier.lower()
        bills = [b for b in bills if s in str(b.get("supplier", {})).lower()
                 or s in str(b.get("billnumber", "")).lower()]

    # ---- build the report ----
    lines_out = []
    lines_out.append("# AroFlo Bill Review — read-only\n")
    lines_out.append(f"In Progress bills: **{len(bills)}**  · "
                     f"learned from {len(processed)} processed bills "
                     f"({len(price_hist)} parts)\n")

    # the actionable signal vs the standing inventory-linking backlog
    SIGNAL = {"price_increase", "line_maths", "field_swap", "pdf_mismatch",
              "edited", "edit_failed", "would_fix", "held"}
    tally = Counter()
    clean_bills = 0
    uncoded_total = 0
    notverified = 0
    notverified_names = []   # new/changed bills the reader couldn't verify (loud Telegram section)
    flags_flat = []          # (billnumber, severity, type, msg) for new/changed bills (Telegram)
    seen = {} if args.all else load_seen(args.state)
    current = {}
    n_new = n_changed = 0
    new_clean = []           # (kind, billnumber) for new/changed bills that are otherwise clean
    tmpdir = tempfile.mkdtemp(prefix="billpdf_") if args.pdf else None

    for b in bills:
        findings, edits = review_bill(b, code_hist, price_hist)
        if args.pdf:
            print(f"  verifying {b.get('billnumber', '?')} against its PDF...", file=sys.stderr)
            pf, pe = pdf_check(conn, b, tmpdir, log_dir=(args.extract_log or None),
                               fix_desc=args.fix_desc,
                               unit_price_fix=args.unit_price_fix)
            findings += pf
            edits += pe
        # apply (or, without --apply, propose) the auto-fixable edits + inserts
        held_bill = bool(hold) and (str(b.get("billnumber", "")) in hold
                                    or str(b.get("billid", "")) in hold)
        # edits the PDF pass didn't trust → manual items, never applied (code audit B1)
        findings += [("info", "held", e["desc"] + e["hold"]) for e in edits if e.get("hold")]
        edits = [e for e in edits if not e.get("hold")]
        if edits:
            if held_bill:
                # on the hold list → never auto-apply; surface as a manual item instead
                findings += [("info", "held", e["desc"]) for e in edits]
            elif args.apply:
                # line UPDATES — group by lineid so a line gets one API call
                by_line = {}
                for e in edits:
                    if e.get("insert"):
                        continue
                    lid = e["line"].get("lineid")
                    grp = by_line.setdefault(lid, {"line": e["line"], "changes": {}, "descs": []})
                    grp["changes"].update(e["changes"])
                    grp["descs"].append(e["desc"])
                for grp in by_line.values():
                    ok, detail = apply_line_edit(conn, b.get("billid"), grp["line"], grp["changes"])
                    for d in grp["descs"]:
                        findings.append(("edit", "edited", d) if ok
                                        else ("flag", "edit_failed", f"{d} — FAILED: {detail}"))
                # line INSERTS — one per missing line
                for e in (e for e in edits if e.get("insert")):
                    ok, detail = apply_line_insert(conn, b.get("billid"), e["fields"])
                    findings.append(("edit", "edited", e["desc"]) if ok
                                    else ("flag", "edit_failed", f"{e['desc']} — FAILED: {detail}"))
            else:
                findings += [("info", "would_fix", e["desc"]) for e in edits]
        signal  = [x for x in findings if x[1] in SIGNAL]
        uncoded = [x for x in findings if x[1] == "needs_code"]
        notes   = [x for x in findings if x[1] == "pdf_note"]
        uncoded_total += len(uncoded)
        if notes:
            notverified += 1
        for _, typ, _ in signal:
            tally[typ] += 1

        bn = b.get("billnumber", "?")
        # new/changed tracking — only new or changed bills reach the Telegram summary
        fp = bill_fingerprint(b, pdf_state=("unverified" if notes else "verified") if args.pdf else "")
        key = b.get("billid", bn)
        current[key] = fp
        prior = seen.get(key)
        kind = "new" if prior is None else ("changed" if prior != fp else "same")
        if kind == "new":
            n_new += 1
        elif kind == "changed":
            n_changed += 1
        if kind != "same" or args.all:
            for sev, typ, msg in signal:
                flags_flat.append((bn, sev, typ, msg))
            if notes:
                notverified_names.append(bn)
            if not signal and not notes:
                new_clean.append((kind, bn))

        tot = f(b.get("totalinc"))
        po = b.get("purchaseorder", {}).get("ordernumber", "")
        tag = {"new": "  🆕", "changed": "  ♻️"}.get(kind, "")
        if not args.pdf:
            pdf_stat = ""
        elif notes:
            pdf_stat = "  ·  ⚠ PDF not verified"
        elif any(t == "pdf_mismatch" for _, t, _ in signal):
            pdf_stat = "  ·  🔴 PDF mismatch"
        else:
            pdf_stat = "  ·  ✓ PDF ok"
        head = f"\n## Bill {bn}{tag}  ·  ${tot:.2f}  ·  PO {po or '—'}{pdf_stat}"

        if not signal:
            clean_bills += 1
            extra = f"  ({len(uncoded)} unlinked)" if uncoded else ""
            lines_out.append(head + f"  ✅ no price/coding issues{extra}")
        else:
            lines_out.append(head)
            for sev, typ, msg in signal:
                icon = {"flag": "🚩", "edit": "✏️", "info": "ℹ️"}.get(sev, "•")
                lines_out.append(f"- {icon} **{typ}** — {msg}")
            if uncoded:
                lines_out.append(f"- 🗂 {len(uncoded)} line(s) not linked to inventory")
        for _, _, msg in notes:
            lines_out.append(f"- ⚠️ {msg}")
        if args.uncoded:
            for _, _, msg in uncoded:
                lines_out.append(f"    - {msg}")

    if tmpdir:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    head_parts = []
    if tally["edited"]:         head_parts.append(f"{tally['edited']} auto-fixed")
    if tally["edit_failed"]:    head_parts.append(f"{tally['edit_failed']} edit FAILED")
    if tally["would_fix"]:      head_parts.append(f"{tally['would_fix']} fixable")
    if tally["pdf_mismatch"]:   head_parts.append(f"{tally['pdf_mismatch']} PDF mismatch(es)")
    if tally["price_increase"]: head_parts.append(f"{tally['price_increase']} price increase(s)")
    if tally["line_maths"]:     head_parts.append(f"{tally['line_maths']} maths error(s)")
    if tally["field_swap"]:     head_parts.append(f"{tally['field_swap']} field swap(s)")
    headline = ", ".join(head_parts) if head_parts else "no price/coding flags"
    nv = f", {notverified} not PDF-verified" if notverified else ""
    lines_out.append(f"\n---\n**{len(bills)} bills · {headline} · "
                     f"{uncoded_total} lines need inventory linking ({clean_bills} bills otherwise clean{nv}).**")

    report = "\n".join(lines_out)
    print(report)
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(f"\n(report written to {args.report})", file=sys.stderr)

    # A run where NOTHING could be PDF-verified isn't a bad scan, it's the reader
    # being down (usually a lapsed claude CLI login). Two symptoms seen: a HANG
    # when token auto-refresh silently stops (Jul 2026), and a clean non-zero
    # exit when the REFRESH token itself expires (Aug 2026). LAST_CLI_ERROR tells
    # the two apart. Partial failure stays quiet on purpose: one unreadable
    # invoice is routine, and alerting on it would cry wolf every night it sits
    # In Progress.
    blind_run = bool(args.pdf) and bool(bills) and notverified == len(bills)

    # Telegram summary — only the new/changed bills; silent if nothing new (unless --all)
    send_failed = False      # a real send that didn't go through (code audit B2)
    if args.telegram or args.dry_run:
        quiet = (n_new + n_changed == 0 and not args.all)
        date_str = datetime.now().strftime("%d %b %Y")
        token, chat = resolve_telegram_creds()
        if quiet and blind_run:
            # Nothing changed AND nothing could be read — the one case where silence
            # is indistinguishable from a clean night. Say so on the bill channel;
            # cron-alert.sh's non-zero alert only reaches the infra bot.
            # Name the actual cause rather than guessing at "likely the login".
            # LAST_CLI_ERROR reads BOTH CLI streams, which is the only way a
            # lapsed login shows up at all — the CLI prints that one to stdout.
            why = (f"<b>Cause:</b> {html.escape(LAST_CLI_ERROR)}" if LAST_CLI_ERROR
                   else "Likely the <code>claude</code> login on the server.")
            alert = (f"🧾 <b>Bill Review — {html.escape(date_str)}</b>\n"
                     f"⛔ <b>PDF verification DOWN</b> — none of the {len(bills)} In Progress "
                     f"bill(s) could be read, so none were checked.\n"
                     f"{why}\n"
                     f"(see <code>~/logs/bill-review.log</code>)")
            if send_telegram(alert, dry_run=args.dry_run, token=token, chat=chat):
                print("Telegram: PDF-verification-down alert sent.", file=sys.stderr)
            elif not args.dry_run:
                send_failed = True
        elif quiet:
            print(f"No new or changed bills ({len(bills)} unchanged) — staying silent.", file=sys.stderr)
        else:
            summary = build_summary(flags_flat, len(bills), clean_bills, uncoded_total, date_str,
                                    n_new=n_new, n_changed=n_changed, new_clean=new_clean,
                                    notverified=notverified, notverified_names=notverified_names)
            if send_telegram(summary, dry_run=args.dry_run, token=token, chat=chat):
                print("Telegram summary sent.", file=sys.stderr)
            elif not args.dry_run:
                send_failed = True
        # persist state after a real (non-dry) run so unchanged bills stay quiet next time.
        # NOT if the send failed: saving would mark these bills 'seen', and tonight's
        # findings would never be reported (code audit B2).
        if args.telegram and not args.dry_run and not send_failed:
            save_seen(args.state, current)
            print(f"State saved ({len(current)} bills tracked).", file=sys.stderr)

    if send_failed:
        print("\nERROR: Telegram send failed — state NOT saved, so these bills report "
              "again next run.", file=sys.stderr)
        return 1

    if blind_run:
        print(f"\nERROR: PDF verification failed on all {len(bills)} bill(s) — "
              f"the reader is down, this run checked nothing.", file=sys.stderr)
        if LAST_CLI_ERROR:
            print(f"Cause: {LAST_CLI_ERROR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
