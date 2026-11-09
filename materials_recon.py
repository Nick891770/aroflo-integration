"""
Materials Reconciliation v2 — monthly stock balance + charge-out check.

v1 answered "are we leaking parts?" by assuming everything bought in a month was
consumed in that month. It wasn't, we hold real stock, and it read as ~100% usage.
v2 tracks stock properly:

    Closing stock = Opening stock + Purchases (at cost) - Materials used (at cost)

Parts that sit in the van/shed stay on the balance and come off later when they
actually go on a job. No per-part tracking needed — the arithmetic self-corrects.

  • Purchases   — bill lines, account 311, at COST (what you paid)
  • Used        — tasktotals.totalmat on jobs COMPLETED in the month, at COST
                  (read off the JOB, not the invoice, so invoice collation state
                  is irrelevant — see the collation note below)
  • WIP         — totalmat sitting on jobs that aren't finished yet
  • Charge-out  — jobs with material cost but $0 material revenue (account 250)

MARKUP IS NOT MEASURED HERE. invoices are uncollated before exporting to Xero, so Xero's
P&L carries the correct labour/materials split. Xero is the source of truth for
markup; this script only answers the stock question.

WHY THE CHARGE-OUT CHECK IS GATED (the v1 false-alarm bug):
Invoices are collated per customer/location and HELD until the client pays, then
uncollated and exported to Xero. While collated they carry one lump-sum line per
job, all coded 200 (Labour), with zero 250 lines. v1 read that as "invoiced with
no materials" and flagged perfectly good jobs. Collation is a TRANSIENT state.
`linkprocesseddate` is the flag: empty = still in flight, skip it and catch it
next cycle. Only invoices that have reached Xero are judged.

Usage:
    python3 materials_recon.py --telegram                 # previous month, send
    python3 materials_recon.py --month 2026/07 --dry-run  # specific month, print
    python3 materials_recon.py --set-opening 12345.67 --as-at 2026-08-31
    python3 materials_recon.py --validate                 # cost-vs-sell sanity check
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

from aroflo_connector import create_connector
# Reuse the bill agent's Telegram plumbing (EA-bot creds, urllib sender).
from bill_review import resolve_telegram_creds, send_telegram

# --- Account codes (confirmed from live data) ---
MATERIAL_COST_ACCTS = {"311"}   # bill side: 311 = Materials cost
MATERIAL_REV_ACCTS = {"250"}    # invoice side: 250 = Materials revenue (200 = Labour)

# Clients whose work is labour-only by nature. A $0-materials invoice from these
# is normal, not a leak. Set LABOUR_ONLY_CLIENTS as a comma-separated list of client names.
# Note this is only a labelling aid — the charge-out check already requires
# material COST > 0 to fire, so genuinely labour-only jobs never trigger it.
LABOUR_ONLY_CLIENTS = {c.strip().lower() for c in os.getenv("LABOUR_ONLY_CLIENTS", "").split(",") if c.strip()}

LEAK_MIN = 1.0                  # flag under-charged jobs down to $1
LIST_LIMIT = 6                  # max line items shown per section in Telegram

# Markup floor. A job charged below this went out at or near cost — the signature
# of a material added to the job WITHOUT a category, which is what applies the
# automatic markup in AroFlo. Both thresholds are configurable per business.
MARKUP_TARGET = float(os.getenv("MARKUP_TARGET", "0.35"))  # target material markup
MARKUP_FLOOR = float(os.getenv("MARKUP_FLOOR", "0.20"))    # below this = worth a look
MARKUP_MIN_COST = 50.0          # ignore trivial jobs where a few dollars skews %

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STOCK_STATE = os.path.join(STATE_DIR, "stock_baseline.json")

# Pull window for the roll-forward. Purchases and usage are summed from the
# stocktake date to the target month, so the balance is recomputed from the
# baseline every run rather than accumulated in a file that can drift.
DEFAULT_HISTORY_START = "2026-01-01"


# ------------------------- helpers -------------------------

def money(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def get_lines(obj, key="lines"):
    lines = obj.get(key) or []
    return list(lines.values()) if isinstance(lines, dict) else lines


def month_key(datestr):
    """'2026/05/08' -> '2026/05'. Tolerant of '-' separators and blanks."""
    s = (datestr or "").replace("-", "/").strip()
    return s[:7] if len(s) >= 7 else ""


def day_key(datestr):
    """'2026/08/19' or '2026-08-19...' -> '2026/08/19'. '' if unusable."""
    s = (datestr or "").replace("-", "/").strip()
    return s[:10] if len(s) >= 10 else ""


def partial_month_delta(bills, tasks, as_at_iso):
    """Stock movement AFTER the stocktake but inside the stocktake's own month.

    The count happens mid-month, so month-level buckets can't place it. Skipping
    the whole month silently drops the rest of it, which bakes a permanent level
    error into every later balance. Counting the whole month double-subtracts
    material that was already off the shelf at the count.

    So the baseline month is handled by DATE: only bills and completed jobs
    dated strictly after the count are rolled forward.
    """
    cut = day_key(as_at_iso)
    if not cut:
        return 0.0, 0.0
    mk = cut[:7]
    bought = used = 0.0
    for b in bills:
        dk = day_key(b.get("dateinvoiced"))
        if not dk or dk[:7] != mk or dk <= cut:
            continue
        for ln in get_lines(b):
            if ln.get("accountcode") in MATERIAL_COST_ACCTS:
                bought += money(ln.get("total"))
    for t in tasks:
        mat = money((t.get("tasktotals") or {}).get("totalmat"))
        if mat <= 0:
            continue
        dk = day_key(t.get("completeddate"))
        if not dk or dk[:7] != mk or dk <= cut:
            continue
        used += mat
    return bought, used


def previous_month(d):
    return f"{d.year - 1}/12" if d.month == 1 else f"{d.year}/{d.month - 1:02d}"


def month_start_iso(mk):
    """'2026/05' -> '2026-05-01' for the API where-clause."""
    return mk.replace("/", "-") + "-01"


def month_end_key(mk):
    """Inclusive upper bound for the roll-forward comparison."""
    return mk


def client_name(t):
    """Client name off a TASK.

    The tasks zone returns client.clientname; the invoices zone returns
    client.orgname. They are not interchangeable, and reading orgname off a task
    silently yields "" -- which is why client names were blank in this report.
    Try both so it survives either zone.
    """
    c = t.get("client") or {}
    return c.get("clientname") or c.get("orgname") or ""


def blank(v):
    """AroFlo returns ' ' (a space) for empty dates, not '' or None."""
    return not str(v or "").strip()


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def short(s, n=46):
    s = " ".join(str(s).split())  # collapse embedded newlines/whitespace
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------- state -------------------------

def load_baseline():
    """{'as_at': 'YYYY-MM-DD', 'value': float, 'note': str} or None."""
    try:
        with open(STOCK_STATE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_baseline(value, as_at, note=""):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {"as_at": as_at, "value": round(float(value), 2), "note": note,
            "recorded": datetime.now().strftime("%Y-%m-%d %H:%M")}
    with open(STOCK_STATE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


# ------------------------- data pulls -------------------------

def fetch_bills_since(conn, start_iso, max_pages=12):
    """Bills with a DATE where-clause, deduped by billid.

    v1 sent no where-clause and silently got a ~10-week window (44 bills);
    with the clause the same account returns 239 back to January. The `status`
    param is ignored by the API (both statuses return an identical set), so
    it is not sent at all — dedupe by billid regardless.
    """
    seen = {}
    for pg in range(1, max_pages + 1):
        resp = conn.request("bills", {"page": str(pg), "join": "lineitems",
                                      "where": f"and|dateinvoiced|>=|{start_iso}"})
        chunk = resp.get("zoneresponse", resp).get("bills", [])
        if not chunk:
            break
        for b in chunk:
            if b.get("billid"):
                seen[b["billid"]] = b
    return list(seen.values())


def fetch_invoices_since(conn, start_iso, max_pages=20):
    """Invoices with dateinvoiced >= start_iso, with line items joined."""
    out = {}
    for pg in range(1, max_pages + 1):
        resp = conn.request("invoices", {"page": str(pg), "join": "lineitems",
                                         "where": f"and|dateinvoiced|>=|{start_iso}"})
        chunk = resp.get("zoneresponse", resp).get("invoices", [])
        if not chunk:
            break
        for iv in chunk:
            out[iv.get("invoiceid") or iv.get("invoicenumber")] = iv
    return list(out.values())


def fetch_tasks_since(conn, start_iso, max_pages=12):
    """Tasks with tasktotals joined, deduped by taskid.

    v1 sent no where-clause and got 22 tasks (the active list), so most invoiced
    jobs fell into 'couldn't auto-verify' and were dropped from the maths. A date
    clause returns 137+. The `status` where-clause is unreliable (four different
    statuses all return 14), so date is the only trustworthy filter. Several date
    fields are merged because each covers a slightly different set.
    """
    seen = {}
    for fld in ("createdutc", "lastupdatedutc", "scheduledstartdate"):
        for pg in range(1, max_pages + 1):
            # No try/except: a failed pull used to be swallowed, silently dropping
            # jobs -> usage under-stated, closing stock over-stated. Fail loudly so
            # cron-alert fires instead (code audit M1, 15 Oct 2026).
            resp = conn.request("tasks", {"page": str(pg), "join": "tasktotals",
                                          "where": f"and|{fld}|>=|{start_iso}"})
            chunk = resp.get("zoneresponse", resp).get("tasks", [])
            if not chunk:
                break
            for t in chunk:
                if t.get("taskid"):
                    seen[t["taskid"]] = t
    return list(seen.values())


# ------------------------- core -------------------------

def build_dataset(conn, start_iso):
    bills = fetch_bills_since(conn, start_iso)
    invoices = fetch_invoices_since(conn, start_iso)
    tasks = fetch_tasks_since(conn, start_iso)
    return bills, invoices, tasks


def purchases_by_month(bills):
    """{month: {'total','to_jobs','no_job','no_job_items','by_task'}} at COST."""
    out = defaultdict(lambda: {"total": 0.0, "to_jobs": 0.0, "no_job": 0.0,
                               "no_job_items": [], "by_task": defaultdict(float)})
    for b in bills:
        mk = month_key(b.get("dateinvoiced"))
        if not mk:
            continue
        label = (b.get("supplier", {}) or {}).get("name") or b.get("billnumber") or "?"
        for ln in get_lines(b):
            if ln.get("accountcode") not in MATERIAL_COST_ACCTS:
                continue
            cost = money(ln.get("total"))
            rec = out[mk]
            rec["total"] += cost
            tid = (ln.get("taskid") or "").strip()
            if tid:
                rec["to_jobs"] += cost
                rec["by_task"][tid] += cost
            else:
                rec["no_job"] += cost
                rec["no_job_items"].append((label, ln.get("description"), cost))
    return out


def usage_by_month(tasks):
    """Materials consumed, at COST, attributed to the month the job COMPLETED.

    tasktotals.totalmat is a current snapshot of material on the job, so a job
    spanning months lands entirely in its completion month. That is the right
    behaviour for stock: parts leave the shelf when the job is finished.
    Jobs with no completion date are WIP and are excluded from usage.
    """
    out = defaultdict(float)
    detail = defaultdict(list)
    wip = 0.0
    wip_jobs = 0
    for t in tasks:
        mat = money((t.get("tasktotals") or {}).get("totalmat"))
        if mat <= 0:
            continue
        done = t.get("completeddate")
        if blank(done):
            wip += mat
            wip_jobs += 1
            continue
        mk = month_key(done)
        if not mk:
            continue
        out[mk] += mat
        detail[mk].append((t.get("jobnumber") or t.get("taskid"), mat,
                           client_name(t)))
    return out, detail, wip, wip_jobs


def charge_out_check(tasks, invoices, target_mk):
    """Jobs completed in the month with material cost but $0 material revenue,
    counting ONLY invoices that have actually reached Xero (linkprocesseddate).

    Also returns dollar-vs-dollar recovery on the jobs that DID charge materials
    — v1 only ever asked 'is there any material line at all', which hid partial
    under-charging entirely.
    """
    # invoice side: material revenue per encoded task id, exported invoices only
    mat_rev = defaultdict(float)
    exported_encs = set()
    inflight_encs = set()
    for iv in invoices:
        exported = not blank(iv.get("linkprocesseddate"))
        for ln in get_lines(iv):
            enc = (ln.get("task", {}) or {}).get("webappEncodedID")
            if not enc:
                continue
            if exported:
                exported_encs.add(enc)
                if ln.get("accountcode") in MATERIAL_REV_ACCTS:
                    mat_rev[enc] += money(ln.get("total"))
            else:
                inflight_encs.add(enc)

    not_charged, under_charged, low_markup = [], [], []
    in_flight = 0
    cost_sum = rev_sum = 0.0
    for t in tasks:
        if month_key(t.get("completeddate")) != target_mk:
            continue
        mat = money((t.get("tasktotals") or {}).get("totalmat"))
        if mat < LEAK_MIN:
            continue
        enc = t.get("webappEncodedID")
        job = t.get("jobnumber") or t.get("taskid")
        client = client_name(t)
        if enc not in exported_encs:
            if enc in inflight_encs:
                in_flight += 1
            continue
        rev = mat_rev.get(enc, 0.0)
        cost_sum += mat
        rev_sum += rev
        if rev <= 0.005:
            not_charged.append((job, client, mat))
        elif mat - rev >= LEAK_MIN:
            # Charged BELOW cost — the LEAK_MIN floor keeps cent-level rounding out.
            under_charged.append((job, client, mat, rev))
        elif mat >= MARKUP_MIN_COST and (rev / mat - 1) < MARKUP_FLOOR:
            # Charged above cost but barely — almost certainly an uncategorised
            # material that never picked up its automatic markup.
            low_markup.append((job, client, mat, rev, rev / mat - 1))
    return {
        "not_charged": sorted(not_charged, key=lambda x: -x[2]),
        "under_charged": sorted(under_charged, key=lambda x: -(x[2] - x[3])),
        "low_markup": sorted(low_markup, key=lambda x: -(x[2] * MARKUP_TARGET - (x[3] - x[2]))),
        "in_flight": in_flight,
        "cost_sum": cost_sum,
        "rev_sum": rev_sum,
    }


def reconcile(conn, target_mk):
    baseline = load_baseline()
    start_iso = (baseline or {}).get("as_at") or DEFAULT_HISTORY_START
    # Roll forward from the month AFTER the stocktake.
    base_mk = month_key(start_iso)

    bills, invoices, tasks = build_dataset(conn, DEFAULT_HISTORY_START)
    pur = purchases_by_month(bills)
    use, use_detail, wip, wip_jobs = usage_by_month(tasks)

    m = pur.get(target_mk, {"total": 0.0, "to_jobs": 0.0, "no_job": 0.0,
                            "no_job_items": [], "by_task": {}})
    used = use.get(target_mk, 0.0)

    # Roll the baseline forward: every month strictly after the stocktake month,
    # up to and including the target month.
    opening = (baseline or {}).get("value")
    rolled_from = None
    if opening is not None:
        rolled = float(opening)
        # The stocktake month is partial — pick it up by date, not by bucket,
        # or the days between the count and month-end vanish for good.
        if target_mk > base_mk:
            part_buy, part_use = partial_month_delta(bills, tasks, start_iso)
            rolled += part_buy - part_use
        months = sorted(set(list(pur.keys()) + list(use.keys())))
        for mk in months:
            if mk <= base_mk or mk >= target_mk:
                continue
            rolled += pur.get(mk, {}).get("total", 0.0) - use.get(mk, 0.0)
        opening_for_month = rolled
        rolled_from = base_mk
    else:
        opening_for_month = None

    # Reporting ON the stocktake month: the count already IS the position at
    # the count date, so only what happened after it may be added. Using the
    # month's full figures here would subtract material already used
    # before he counted.
    partial_month = opening_for_month is not None and target_mk == base_mk
    if partial_month:
        part_buy, part_use = partial_month_delta(bills, tasks, start_iso)
        closing = opening_for_month + part_buy - part_use
    else:
        closing = (opening_for_month + m["total"] - used
                   if opening_for_month is not None else None)

    return {
        "month": target_mk,
        "baseline": baseline,
        "partial_month": partial_month,
        "rolled_from": rolled_from,
        "opening": opening_for_month,
        "purchased": m["total"],
        "to_jobs": m["to_jobs"],
        "no_job": m["no_job"],
        "no_job_items": sorted(m["no_job_items"], key=lambda x: -x[2]),
        "used": used,
        "used_jobs": sorted(use_detail.get(target_mk, []), key=lambda x: -x[1]),
        "closing": closing,
        "wip": wip,
        "wip_jobs": wip_jobs,
        "charge": charge_out_check(tasks, invoices, target_mk),
        "n_bills": len(bills),
        "n_invoices": len(invoices),
        "n_tasks": len(tasks),
    }


# ------------------------- output -------------------------

def build_summary(r):
    mk = r["month"].replace("/", "-")
    c = r["charge"]
    L = [f"📦 <b>Materials — {mk}</b>\n"]

    # --- stock movement ---
    L.append("<b>Stock movement</b>")
    if r["opening"] is not None:
        L.append(f" Opening        ${r['opening']:>10,.0f}")
    else:
        L.append(" Opening         <i>not set</i>")
    L.append(f" + Bought       ${r['purchased']:>10,.0f}")
    L.append(f" − Used on jobs ${r['used']:>10,.0f}")
    if r["closing"] is not None:
        L.append(f" <b>= Closing      ${r['closing']:>10,.0f}</b>")
    else:
        L.append(" = Closing       <i>needs a stocktake</i>")
    net = r["purchased"] - r["used"]
    L.append(f"\nNet movement: <b>{'+' if net >= 0 else '−'}${abs(net):,.0f}</b> "
             f"({'stock built up' if net >= 0 else 'stock drawn down'})")

    if r.get("partial_month"):
        L.append(f"<i>⚠ Part month. The stocktake was counted on "
                 f"{r['baseline'].get('as_at')}, so Bought and Used above cover "
                 f"the whole month but Closing counts only what moved after the "
                 f"count. First full month is the next one.</i>")
    elif r["baseline"] and r["rolled_from"]:
        L.append(f"<i>Opening rolled forward from stocktake "
                 f"{r['baseline'].get('as_at')} (${r['baseline'].get('value'):,.0f})</i>")

    # --- purchases detail ---
    L.append(f"\n<b>Bought</b> ${r['purchased']:,.0f}")
    L.append(f" • Against a job: ${r['to_jobs']:,.0f}")
    L.append(f" • No job (van/shed): ${r['no_job']:,.0f}")
    if r["no_job_items"]:
        for sup, desc, cost in r["no_job_items"][:LIST_LIMIT]:
            L.append(f"    ${cost:,.0f}  {esc(short(desc))} <i>({esc(sup)})</i>")
        extra = len(r["no_job_items"]) - LIST_LIMIT
        if extra > 0:
            L.append(f"    …+{extra} more")

    # --- usage detail ---
    if r["used_jobs"]:
        L.append(f"\n<b>Used on jobs completed this month</b> ${r['used']:,.0f}")
        for job, mat, client in r["used_jobs"][:LIST_LIMIT]:
            L.append(f" • Job {esc(job)} ${mat:,.0f} <i>{esc(short(client, 24))}</i>")
        extra = len(r["used_jobs"]) - LIST_LIMIT
        if extra > 0:
            L.append(f" • …+{extra} more")

    # --- charge-out ---
    if c["not_charged"]:
        tot = sum(x[2] for x in c["not_charged"])
        L.append(f"\n🔴 <b>Not charged out</b> — ${tot:,.0f} over "
                 f"{len(c['not_charged'])} job(s):")
        for job, client, mat in c["not_charged"][:LIST_LIMIT]:
            tag = " <i>(usually labour-only)</i>" if client.lower() in LABOUR_ONLY_CLIENTS else ""
            L.append(f" • Job {esc(job)} ${mat:,.0f} materials, "
                     f"$0 charged <i>{esc(short(client, 20))}</i>{tag}")
    if c["under_charged"]:
        gap = sum(x[2] - x[3] for x in c["under_charged"])
        L.append(f"\n🟠 <b>Under-charged</b> — ${gap:,.0f} short over "
                 f"{len(c['under_charged'])} job(s):")
        for job, client, mat, rev in c["under_charged"][:LIST_LIMIT]:
            L.append(f" • Job {esc(job)} cost ${mat:,.0f}, charged ${rev:,.0f}")
    if c["low_markup"]:
        shortfall = sum(m * MARKUP_TARGET - (r - m) for _, _, m, r, _ in c["low_markup"])
        L.append(f"\n🟡 <b>Charged at ~cost — check the category</b> "
                 f"({len(c['low_markup'])} job(s), ${shortfall:,.0f} short of {MARKUP_TARGET:.0%}):")
        for job, client, mat, rev, mu in c["low_markup"][:LIST_LIMIT]:
            L.append(f" • Job {esc(job)} ${mat:,.0f} → ${rev:,.0f} "
                     f"= <b>{mu*100:+.0f}%</b> markup")
        L.append("<i>A material added to a job without a category gets no "
                 "automatic markup.</i>")
    if not c["not_charged"] and not c["under_charged"] and not c["low_markup"]:
        L.append("\n✅ Charge-out: nothing missed on invoices that reached Xero")
    if c["cost_sum"] > 0:
        mu = c["rev_sum"] / c["cost_sum"] * 100 - 100
        L.append(f"<i>Checked ${c['cost_sum']:,.0f} cost vs ${c['rev_sum']:,.0f} "
                 f"charged → {mu:+.0f}% markup this month.</i>")
        L.append("<i>Cross-check only — Xero is the source of truth for markup.</i>")

    # --- informational ---
    if c["in_flight"]:
        L.append(f"\n⏳ {c['in_flight']} job(s) skipped — invoice still collated / "
                 f"not yet in Xero. Checked next cycle.")
    if r["wip"]:
        L.append(f"🔧 ${r['wip']:,.0f} of material sitting on {r['wip_jobs']} "
                 f"unfinished job(s) — inside the closing balance above.")
    L.append(f"\n<i>{r['n_bills']} bills · {r['n_invoices']} invoices · "
             f"{r['n_tasks']} jobs scanned</i>")
    return "\n".join(L)


def strip_html(s):
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        s = s.replace(tag, "")
    return s


# ------------------------- validation -------------------------

def validate(conn):
    """Sanity-check that tasktotals.totalmat is COST, not sell price.

    If it were sell, it would sit noticeably above the material cost billed
    against the same jobs. Compares like-for-like on jobs whose materials were
    all bought in the window.
    """
    bills, _, tasks = build_dataset(conn, DEFAULT_HISTORY_START)
    pur = purchases_by_month(bills)
    cost_by_task = defaultdict(float)
    for rec in pur.values():
        for tid, v in rec["by_task"].items():
            cost_by_task[tid] += v

    rows = []
    for t in tasks:
        tid = t.get("taskid")
        mat = money((t.get("tasktotals") or {}).get("totalmat"))
        billed = cost_by_task.get(tid, 0.0)
        if mat > 0 and billed > 0:
            rows.append((t.get("jobnumber") or tid, billed, mat))

    print(f"\n{'job':>10} {'bill cost':>12} {'totalmat':>12} {'ratio':>8}")
    print("-" * 46)
    tb = tm = 0.0
    for job, billed, mat in sorted(rows, key=lambda x: -x[1])[:20]:
        print(f"{str(job):>10} {billed:>12,.2f} {mat:>12,.2f} {mat/billed:>8.2f}")
    for _, billed, mat in rows:
        tb += billed
        tm += mat
    print("-" * 46)
    if tb:
        ratio = tm / tb
        print(f"{'TOTAL':>10} {tb:>12,.2f} {tm:>12,.2f} {ratio:>8.2f}  "
              f"({len(rows)} jobs)")
        print(f"\n  ratio ~1.0  => totalmat is COST  (design assumption holds)")
        print(f"  ratio ~1.3+ => totalmat is SELL  (usage must be de-marked-up)")
        print(f"\n  MEASURED: {ratio:.2f} -> "
              f"{'COST, assumption holds' if ratio < 1.15 else 'SELL — DO NOT SHIP, fix usage basis'}")
    else:
        print("  no overlapping jobs to compare")


# ------------------------- main -------------------------

def main():
    ap = argparse.ArgumentParser(description="Monthly materials reconciliation v2")
    ap.add_argument("--month", help="Target month YYYY/MM (default: previous month)")
    ap.add_argument("--telegram", action="store_true", help="Send to Telegram ")
    ap.add_argument("--dry-run", action="store_true", help="Print instead of sending")
    ap.add_argument("--set-opening", type=float, metavar="VALUE",
                    help="Record the stocktake value (ex-GST) and exit")
    ap.add_argument("--as-at", metavar="YYYY-MM-DD",
                    help="Date the stocktake was counted (with --set-opening)")
    ap.add_argument("--note", default="", help="Optional note with --set-opening")
    ap.add_argument("--validate", action="store_true",
                    help="Check that tasktotals.totalmat is cost, not sell")
    args = ap.parse_args()

    if args.set_opening is not None:
        if not args.as_at:
            sys.exit("--set-opening needs --as-at YYYY-MM-DD (the count date)")
        data = save_baseline(args.set_opening, args.as_at, args.note)
        print(f"Stocktake recorded: ${data['value']:,.2f} as at {data['as_at']}")
        print(f"  -> {STOCK_STATE}")
        return

    conn = create_connector()

    if args.validate:
        validate(conn)
        return

    target = args.month or previous_month(datetime.now())
    print(f"Reconciling materials for {target} …")
    r = reconcile(conn, target)
    summary = build_summary(r)
    print("\n" + strip_html(summary))

    if not r["baseline"]:
        print("\n[!] No stocktake recorded — closing balance unavailable.")
        print("    Set it with: --set-opening 12345.67 --as-at 2026-08-31")

    if args.telegram or args.dry_run:
        token, chat = resolve_telegram_creds()
        sent = send_telegram(summary, dry_run=args.dry_run, token=token, chat=chat)
        if args.telegram and not args.dry_run and not sent:
            # Exit non-zero so cron-alert.sh fires; this used to exit 0 and the
            # month's report just never arrived (code audit M2, 15 Oct 2026).
            print("ERROR: Telegram send failed — monthly report not delivered.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
