"""
AroFlo Purchase Order Report — READ-ONLY.

Surfaces the "I forgot to approve the PO" pile. A PO sits In Progress until you
approve it; once approved it auto-closes when its bill is pushed to Xero. So any
In Progress PO whose bill is already Approved or Processed is just waiting on a
click — and it's invisible unless you go looking.

Buckets:
  ready    — PO In Progress, its bill is Approved/Processed  → approve the PO
  billed   — PO In Progress, bill exists but still In Progress → the bill review
             pass handles it first; PO follows after
  awaiting — PO In Progress, no bill yet → waiting on the supplier's invoice
  noise    — PO In Progress with no lines at all → likely an abandoned/empty order

Writes nothing. Approving stays a human decision.

Usage:
    python po_report.py                 # print the report
    python po_report.py --telegram      # also send it
    python po_report.py --dry-run       # print what WOULD be sent
    python po_report.py --age 14        # only POs older than N days in 'awaiting'
"""

import argparse
import html
import sys
from collections import defaultdict
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from aroflo_connector import AroFloConnector
from config import AROFLO_HOST_IP

from bill_review import f, resolve_telegram_creds, send_telegram


def fetch_zone(conn, zone, status=None, join="lineitems", max_pages=6):
    """Page through a zone, optionally filtered by status. AroFlo's status
    where-clause is loose, so filter client-side too (same as fetch_bills)."""
    out = []
    for pg in range(1, max_pages + 1):
        params = {"join": join, "page": str(pg)}
        if status:
            params["where"] = f"and|status|=|{status}"
        resp = conn.request(zone, params)
        rows = resp.get("zoneresponse", {}).get(zone, []) if isinstance(resp, dict) else []
        if not rows:
            break
        kept = rows
        if status:
            kept = [r for r in rows if str(r.get("status", "")).strip().lower() == status.lower()]
        out.extend(kept)
        # page-size test must use the RAW row count — the status where-clause is loose,
        # so a full page can filter down to a handful and we'd stop paging far too early
        if len(rows) < 25:
            break
    return out


def days_since(datestr):
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return (datetime.now() - datetime.strptime(str(datestr)[:10], fmt)).days
        except (ValueError, TypeError):
            continue
    return None


def supplier_of(rec):
    return (rec.get("supplier") or {}).get("orgname") or "?"


def esc(v):
    """Telegram HTML: a raw '&' or '<' (e.g. 'Hare & Forbes') makes the whole send
    fail with 'can't parse entities' (code audit P1, 15 Oct 2026)."""
    return html.escape(str(v), quote=False)


def build_report(conn, min_age=0):
    # index every bill by the PO it belongs to
    bills_by_po = defaultdict(list)
    for status in ("in progress", "approved", "processed"):
        for b in fetch_zone(conn, "bills", status=status, join="", max_pages=6):
            poid = (b.get("purchaseorder") or {}).get("purchaseorderid")
            if poid:
                bills_by_po[poid].append(b)

    pos = fetch_zone(conn, "purchaseorders", status="in progress", join="lineitems")

    buckets = {"ready": [], "billed": [], "awaiting": [], "noise": []}
    for p in pos:
        poid = p.get("purchaseorderid")
        linked = bills_by_po.get(poid, [])
        age = days_since(p.get("purchasedate"))
        row = {
            "num": p.get("ordernumber", "?"),
            "supplier": supplier_of(p),
            "total": f(p.get("totalinc")),
            "age": age,
            "lines": len(p.get("lines") or []),
            "bills": [(b.get("billnumber"), str(b.get("status", ""))) for b in linked],
        }
        settled = [s for _, s in row["bills"] if s in ("approved", "processed")]
        if settled:
            buckets["ready"].append(row)
        elif linked:
            buckets["billed"].append(row)
        elif row["lines"] == 0:
            buckets["noise"].append(row)
        elif age is None or age >= min_age:
            buckets["awaiting"].append(row)
    for k in buckets:
        buckets[k].sort(key=lambda r: -(r["age"] or 0))
    return buckets, len(pos)


def render(buckets, total, min_age):
    L = [f"📋 <b>Purchase Orders — {total} In Progress</b>"]

    if buckets["ready"]:
        L.append(f"\n✅ <b>{len(buckets['ready'])} ready to approve</b> — bill already settled")
        for r in buckets["ready"]:
            bl = ", ".join(f"{esc(n)} ({esc(s)})" for n, s in r["bills"])
            age = f"{r['age']}d" if r["age"] is not None else "?"
            L.append(f"  • <b>{esc(r['num'])}</b> {esc(r['supplier'])} ${r['total']:,.2f} · {age} · bill {bl}")

    if buckets["billed"]:
        L.append(f"\n🧾 <b>{len(buckets['billed'])} bill in progress</b> — review the bill, then the PO")
        for r in buckets["billed"][:12]:
            bl = ", ".join(esc(n) for n, _ in r["bills"])
            L.append(f"  • {esc(r['num'])} {esc(r['supplier'])} ${r['total']:,.2f} · bill {bl}")
        if len(buckets["billed"]) > 12:
            L.append(f"  …and {len(buckets['billed']) - 12} more")

    if buckets["noise"]:
        L.append(f"\n🗑 <b>{len(buckets['noise'])} empty</b> — no line items, likely abandoned")
        L.append("  " + ", ".join(esc(r["num"]) for r in buckets["noise"][:20]))

    aw = buckets["awaiting"]
    if aw:
        old = [r for r in aw if (r["age"] or 0) >= 30]
        L.append(f"\n⏳ <b>{len(aw)} awaiting supplier invoice</b>"
                 + (f" ({len(old)} over 30 days)" if old else ""))
        for r in old[:10]:
            L.append(f"  • {esc(r['num'])} {esc(r['supplier'])} ${r['total']:,.2f} · {r['age']}d")

    if not any(buckets.values()):
        L.append("\nNothing sitting — all In Progress POs are waiting on invoices.")
    if min_age:
        L.append(f"\n<i>'awaiting' filtered to ≥{min_age} days old</i>")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Read-only AroFlo purchase order report")
    ap.add_argument("--telegram", action="store_true", help="send the summary to Telegram")
    ap.add_argument("--dry-run", action="store_true", help="print the summary instead of sending")
    ap.add_argument("--age", type=int, default=0,
                    help="only list 'awaiting' POs at least N days old")
    args = ap.parse_args()

    conn = AroFloConnector(host_ip=AROFLO_HOST_IP or None)
    print("Fetching bills and In Progress purchase orders...", file=sys.stderr)
    buckets, total = build_report(conn, min_age=args.age)
    msg = render(buckets, total, args.age)

    print(msg.replace("<b>", "").replace("</b>", "")
             .replace("<i>", "").replace("</i>", ""))

    if args.telegram or args.dry_run:
        token, chat = resolve_telegram_creds()
        sent = send_telegram(msg, dry_run=args.dry_run, token=token, chat=chat)
        if args.telegram and not args.dry_run and not sent:
            print("ERROR: Telegram send failed.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
