"""
Stocktake helper — build a count sheet, then total it up.

The tracker only needs ONE number from the count: the total value of stock on
hand at cost, ex-GST. Everything else is optional detail that makes the catalogue
work easier later.

The count sheet is pre-filled with what you've actually been buying (from the
bills), with unit costs already in, so you're not looking prices up in the shed.
Blank rows at the bottom for anything not on the list.

Usage:
    python3 stocktake.py --sheet                 # -> stocktake_2026-08-17.csv
    python3 stocktake.py --tally <file.csv>      # total it, then set the opening balance
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aroflo_connector import create_connector  # noqa: E402
import materials_recon as MR  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BLANK_ROWS = 40
HISTORY_START = "2026-01-01"

COLUMNS = ["Location", "Item", "Part No", "Unit cost ex-GST", "Qty on hand",
           "Value", "Notes"]


def clean_headers(fieldnames):
    """Strip BOM junk Excel leaves on the first column name.

    Excel re-encodes the byte-order mark as literal text every time it saves,
    so a round-tripped sheet can carry a real BOM *and* one or more copies of
    "i>>?" glued to the first header. Left alone, the header check fails and
    the tally refuses to run on a sheet that is actually fine.
    """
    return [(f or "").lstrip("﻿ï»¿").strip()
            for f in (fieldnames or [])]


def unit_costs(conn):
    """Weighted-average unit cost per item, straight off the bills.

    The line fields are `qty` and `price` — NOT `quantity`. Reading the wrong
    name silently yields 1.0 per line, which turns a box of 10 plug tops at
    $5.60 into a "unit cost" of $56. Everything here is priced per UNIT, so a
    pack buy has to be divided out. Guard below shouts if qty ever goes missing.
    """
    bills = MR.fetch_bills_since(conn, HISTORY_START)
    times = Counter()
    spend = defaultdict(float)
    nojob = defaultdict(float)
    qty = defaultdict(float)
    pn, last = {}, {}
    lines_seen = no_qty = 0
    for b in bills:
        for ln in MR.get_lines(b):
            if ln.get("accountcode") not in MR.MATERIAL_COST_ACCTS:
                continue
            desc = " ".join(str(ln.get("description") or "").split())
            if not desc:
                continue
            cost = MR.money(ln.get("total"))
            price = MR.money(ln.get("price"))
            q = MR.money(ln.get("qty"))
            lines_seen += 1
            if q <= 0:
                # No quantity on the line. Back it out of the unit price if we
                # can; only assume a single unit as a last resort.
                no_qty += 1
                q = (cost / price) if price > 0 else 1.0
            times[desc] += 1
            spend[desc] += cost
            qty[desc] += q
            if price > 0:
                last[desc] = price
            if not (ln.get("taskid") or "").strip():
                nojob[desc] += cost
            if ln.get("ordercode"):
                pn[desc] = ln["ordercode"]

    if lines_seen and no_qty / lines_seen > 0.20:
        print(f"  [!] WARNING: {no_qty} of {lines_seen} material lines had no "
              f"usable qty.\n      Unit costs below are unreliable — do NOT "
              f"tally without checking them.")
    return {"times": times, "spend": spend, "nojob": nojob, "qty": qty,
            "pn": pn, "last": last}


def priced(desc, spend, qty, last):
    """(unit cost, warning-or-None) for one item."""
    unit = spend[desc] / qty[desc] if qty[desc] else 0.0
    lp = last.get(desc, 0.0)
    warn = None
    # A weighted average well off the most recent billed price means the pack
    # size or the price moved, or two catalogue entries share a description.
    if unit > 0 and lp > 0 and abs(unit - lp) / max(unit, lp) > 0.20:
        warn = f"CHECK: last billed ${lp:,.2f}"
    return unit, warn


def build_sheet(conn, outfile, top):
    """Prompt the items most likely to be ON THE SHELF.

    Ranking by total spend is wrong — it surfaces one-off job buys (a drinking
    fountain, a PAC tank) that were never stock. What you HOLD is what you buy
    repeatedly, and especially what you buy against NO job (straight to the van
    or shed). So rank by: bought on 2+ occasions, then by no-job spend.
    """
    prices = unit_costs(conn)
    times, spend, nojob, qty, pn, last = (prices[k] for k in
        ("times", "spend", "nojob", "qty", "pn", "last"))

    # repeat purchases first, then anything bought to no job at all
    repeat = [d for d in times if times[d] >= 2 or nojob[d] > 0]
    ranked = sorted(repeat, key=lambda d: (-times[d], -nojob[d]))[:top]

    with open(outfile, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for note in ("# Count the shed and both vans. Ex-GST, at COST.",
                     "# Accurate on the big gear; ballpark the fittings.",
                     "# Leave Value and Location blank - only Qty matters.",
                     "# Listed = bought repeatedly or bought against no job.",
                     "# Duplicates under 2 part nos: count both, --reprice merges."):
            w.writerow([note] + [""] * (len(COLUMNS) - 1))
        w.writerow([])
        for desc in ranked:
            unit, warn = priced(desc, spend, qty, last)
            note = f"bought {times[desc]}x"
            if nojob[desc] > 0:
                note += f", ${nojob[desc]:,.0f} to no job"
            if warn:
                note += f" — {warn}"
            w.writerow(["", desc[:70], pn.get(desc, ""),
                        f"{unit:.2f}" if unit else "", "", "", note])
        for _ in range(BLANK_ROWS):
            w.writerow([""] * len(COLUMNS))

    print(f"Count sheet -> {outfile}")
    print(f"  {len(ranked)} prompted items + {BLANK_ROWS} blank rows.")
    print(f"  Listed because they were bought repeatedly, or bought against no")
    print(f"  job (= straight to the van/shed) since {HISTORY_START}.")
    print("  Unit costs pre-filled from your bills — override any that look wrong.")
    print("\n  Fill in 'Qty on hand'. Leave 'Value' blank.")
    print("  Then:  python3 stocktake.py --tally " + os.path.basename(outfile))


MERGE_FILE = os.path.join(HERE, "item_merges.json")


def load_merges():
    """{drop_code: keep_code}, {keep_code: [all codes]} — same item, many entries."""
    if not os.path.exists(MERGE_FILE):
        return {}, {}
    with open(MERGE_FILE, encoding="utf-8") as fh:
        spec = json.load(fh)
    to_keep, group = {}, {}
    for m in spec.get("merges", []):
        keep = str(m["keep"]).strip().upper()
        codes = [keep]
        for d in m.get("drop", []):
            d = str(d).strip().upper()
            to_keep[d] = keep
            codes.append(d)
        group[keep] = codes
    return to_keep, group


def reprice(conn, path, outfile):
    """Refresh unit costs on a sheet that's already been counted.

    Keeps every quantity, location and note you typed — only the cost column
    changes. Writes a NEW file; the sheet you counted on is never touched.
    Match is on Part No first, then the item description.
    """
    p = unit_costs(conn)
    spend, qty, last, pn = p["spend"], p["qty"], p["last"], p["pn"]
    by_pn = {}
    for desc, code in pn.items():
        if code and qty[desc]:
            by_pn.setdefault(str(code).strip().upper(), desc)

    to_keep, group = load_merges()
    # Blended cost for a merged group: total spend over total units, across
    # every code. Fairer than picking one wholesaler's price.
    blended = {}
    for keep, codes in group.items():
        descs = [by_pn[c] for c in codes if c in by_pn]
        t = sum(spend[d] for d in descs)
        q = sum(qty[d] for d in descs)
        if q:
            blended[keep] = t / q

    with open(path, "r", encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        cols = clean_headers(rdr.fieldnames)
        rows = [{c: v for c, v in zip(cols, r.values())} for r in rdr]
    if "Unit cost ex-GST" not in cols:
        sys.exit(f"{os.path.basename(path)} has no 'Unit cost ex-GST' column.")

    # Fold merged rows together FIRST, so the count lands on one row and the
    # blended price applies to all of it. Counting both piles is fine.
    merged_msgs = []
    keep_rows = {}
    for row in rows:
        c = (row.get("Part No") or "").strip().upper()
        if c in group:
            keep_rows[c] = row
    for row in rows:
        c = (row.get("Part No") or "").strip().upper()
        keep = to_keep.get(c)
        if not keep or keep not in keep_rows:
            continue
        q = MR.money(row.get("Qty on hand"))
        if q <= 0:
            continue
        kr = keep_rows[keep]
        kq = MR.money(kr.get("Qty on hand"))
        kr["Qty on hand"] = f"{kq + q:g}"
        row["Qty on hand"] = ""
        row["Notes"] = (f"MERGED into {keep} — same item"
                        f" ({(row.get('Notes') or '').split('—')[0].strip()})").strip()
        merged_msgs.append(f"{c} x{q:g} -> {keep} (now x{kq + q:g})")

    changed = unmatched = 0
    biggest = []
    for row in rows:
        item = (row.get("Item") or "").strip()
        if not item or item.startswith("#"):
            continue
        code = (row.get("Part No") or "").strip().upper()
        desc = by_pn.get(code) or (item if item in spend else None)
        if desc is None:
            # Sheet truncates descriptions to 70 chars — match on that prefix.
            desc = next((d for d in spend if d[:70] == item), None)
        if desc is None:
            if (row.get("Qty on hand") or "").strip():
                unmatched += 1
            continue
        was = MR.money(row.get("Unit cost ex-GST"))
        now, warn = priced(desc, spend, qty, last)
        if code in blended:
            now = blended[code]
            warn = None  # a merged group is expected to span two prices
        if not now:
            continue
        row["Unit cost ex-GST"] = f"{now:.2f}"
        if warn and warn not in (row.get("Notes") or ""):
            row["Notes"] = ((row.get("Notes") or "") + f" — {warn}").strip(" —")
        if abs(now - was) > 0.005:
            changed += 1
            q = MR.money(row.get("Qty on hand"))
            biggest.append((abs((now - was) * q), item, was, now, q))

    with open(outfile, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    print(f"\n  Repriced -> {outfile}")
    print(f"  {changed} unit cost(s) updated.")
    if merged_msgs:
        print(f"\n  {len(merged_msgs)} duplicate row(s) folded together "
              f"(blended cost applied):")
        for m in merged_msgs:
            print(f"      {m}")
    else:
        print("  Your quantities are untouched.")
    if unmatched:
        print(f"  [!] {unmatched} counted row(s) had no match in the bills "
              f"(write-ins) — their costs were left as you typed them.")
    biggest.sort(reverse=True)
    if biggest and biggest[0][0] > 0:
        print("\n  Biggest corrections by value on hand:")
        for delta, item, was, now, q in biggest[:10]:
            if delta <= 0:
                break
            print(f"    {item[:40]:<40} ${was:>8,.2f} -> ${now:>8,.2f} "
                  f"x{q:g} = ${delta:,.0f}")
    print(f"\n  Then:  python3 stocktake.py --tally {os.path.basename(outfile)}")


def tally(path):
    total = 0.0
    counted = skipped = 0
    problems = []
    with open(path, "r", encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        rdr.fieldnames = clean_headers(rdr.fieldnames)
        # Fail loudly on a mangled header. Without this a renamed column just
        # yields $0.00, which reads like "no stock" instead of "broken file".
        missing = [c for c in ("Item", "Qty on hand", "Unit cost ex-GST", "Value")
                   if c not in (rdr.fieldnames or [])]
        if missing:
            sys.exit(f"Count sheet header is wrong — missing column(s): "
                     f"{', '.join(missing)}\n"
                     f"  found: {', '.join(rdr.fieldnames or ['(nothing)'])}\n"
                     f"  Don't edit the top row; re-generate with --sheet if needed.")
        for i, row in enumerate(rdr, start=2):
            item = (row.get("Item") or "").strip()
            if not item or item.startswith("#"):
                continue
            qty_s = (row.get("Qty on hand") or "").strip()
            if not qty_s:
                skipped += 1
                continue
            val_s = (row.get("Value") or "").strip()
            if val_s:
                v = MR.money(val_s)
            else:
                unit = MR.money(row.get("Unit cost ex-GST"))
                q = MR.money(qty_s)
                if unit <= 0:
                    problems.append((i, item, "no unit cost and no value"))
                    continue
                v = unit * q
            if v < 0:
                problems.append((i, item, f"negative value {v:,.2f}"))
                continue
            total += v
            counted += 1

    print(f"\n  Lines counted        : {counted}")
    print(f"  Lines left blank     : {skipped}  (not counted)")
    if problems:
        print(f"\n  [!] {len(problems)} line(s) need attention:")
        for ln, item, why in problems[:15]:
            print(f"      row {ln}: {item[:44]} — {why}")
    print(f"\n  TOTAL STOCK ON HAND (ex-GST, at cost): ${total:,.2f}")
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\n  Record it with:")
    print(f"    python3 materials_recon.py --set-opening {total:.2f} --as-at {today}")
    return total


def main():
    ap = argparse.ArgumentParser(description="Stocktake count sheet and tally")
    ap.add_argument("--sheet", action="store_true", help="Generate the count sheet")
    ap.add_argument("--top", type=int, default=120,
                    help="How many prompted items (default 120)")
    ap.add_argument("--out", help="Output path for the sheet")
    ap.add_argument("--tally", metavar="FILE", help="Total up a filled-in sheet")
    ap.add_argument("--reprice", metavar="FILE",
                    help="Refresh unit costs on a counted sheet, keeping your "
                         "quantities (writes a new file)")
    args = ap.parse_args()

    if args.tally:
        tally(args.tally)
        return
    if args.reprice:
        out = args.out or args.reprice.replace(".csv", "") + "_repriced.csv"
        reprice(create_connector(), args.reprice, out)
        return
    if args.sheet:
        out = args.out or os.path.join(
            HERE, f"stocktake_{datetime.now().strftime('%Y-%m-%d')}.csv")
        build_sheet(create_connector(), out, args.top)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
