# AroFlo Integration Suite

[![CI](https://github.com/Nick891770/aroflo-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/Nick891770/aroflo-integration/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Python automation for [AroFlo](https://www.aroflo.com/), the job-management system a lot of Australian trade businesses run on. It proofreads job cards, checks every supplier bill against the original PDF invoice, finds purchase orders waiting on a click, reconciles materials and stock, and builds the monthly scorecard.

It runs every night on a small self-hosted Linux server for an electrical contracting business in Brisbane, and sends its findings to Telegram for a person to act on.

## The problem

A small trade business loses money in the gaps between systems:

- **Supplier bills.** AroFlo's AI bill scanner reads wholesaler invoices, but it gets things wrong: ignoring a wholesaler's "per 100" price column (34 m of cable came through as $7,367 instead of $73.67), freight coded as materials, loyalty-point text stuck onto part descriptions and passed through to the client's invoice. Nobody re-checks every line by hand.
- **Job cards.** Techs write job notes on their phones. Typos go straight onto client invoices.
- **Purchase orders.** An approved bill with an unapproved PO is invisible unless someone goes looking.
- **Materials.** "Are we under-charging for parts?" can't be answered without matching purchases to stock to what was billed.
- **Reporting.** The monthly scorecard was a manual export-and-paste job.

## What it does

| Tool | What it does | Writes to AroFlo? |
|------|--------------|-------------------|
| `bill_review.py` | Re-checks every In Progress bill: account coding, price rises vs. last paid, line arithmetic, unlinked items. With `--pdf`, a headless Claude reads the supplier's PDF invoice on its own and compares it line by line with what the scanner entered. | Only with `--apply` (line fixes). **Never approves a bill.** |
| `job_monitor.py` | Polls for completed jobs, proofreads them, marks them Ready to Invoice and leaves an audit note. SQLite stops anything being processed twice. | Only with `--apply` |
| `proofread_and_mark_ready.py` | End-of-period run: proofread, fix descriptions, list manual timesheet fixes, mark ready. | Only with `--apply` |
| `po_report.py` | Lists POs whose bill is already approved, so they're only waiting on a click. | No |
| `materials_recon.py` | Monthly stock balance (opening + purchases − used = closing) and jobs where materials went out at or near cost. | No |
| `stocktake.py` | Builds a count sheet pre-filled from recent purchases, then totals it. | No |
| `main.py report / update` | Monthly revenue, profit, job count and client-concentration metrics, written to an Excel scorecard. | No |

## Design decisions

- **Read-only by default.** Every tool that can write runs as a dry run unless you pass `--apply`. Approving bills and POs stays a human decision.
- **Two independent reads of each invoice.** The bill check doesn't trust the scanner's output. It extracts the PDF separately and compares the two, with rounding-aware tolerances (scanner stores 2dp, invoices often print 4dp).
- **Fails loudly.** If the PDF reader is down and no bill can be verified, the nightly summary says so instead of reporting a clean night. Bill fingerprints include verification state, so bills stored during an outage are re-reported once checking works again.
- **Only alerts on what changed.** Each bill's reviewable content is hashed, and the Telegram summary covers only new or changed bills.
- **Learns from history.** Expected account codes and last-paid prices come from the business's own processed bills, not a hard-coded list.

## AroFlo API lessons

These cost real time to find, and none of them are in the docs:

- **A request with no `where` clause doesn't return everything.** It returns a small recent slice, and pagination stops after page 1. No error, so the code looks right while monthly totals quietly come out at $0. Always filter by date.
- **Timesheet notes are read-only.** Updates return `updatetotal:1` and are silently ignored. The proofreader prints a manual correction list instead.
- **Field names differ from the docs.** Invoices use `totalex` and `lines`, not `totalexgst` and `lineitems`, and the client sits under `client.orgname`.
- **Multi-condition date filters aren't supported**, so the upper date bound is applied client-side.
- **Signing.** Requests are HMAC-SHA512 signed over a `+`-joined payload in the exact order used by AroFlo's Postman pre-request script. `tests/test_connector_auth.py` pins that down.

## Tests

```bash
pip install -r requirements.txt pytest
pytest -q tests
```

The tests cover bill-review rules, change detection, scorecard maths and request signing. They run offline with fake credentials, and CI runs them on every push alongside a [gitleaks](https://github.com/gitleaks/gitleaks) secrets scan.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` with your AroFlo API credentials (found in Site Administration > Settings > General > AroFlo API):

```
AROFLO_ORG_NAME=your_org_encoded
AROFLO_USERNAME=your_username_encoded
AROFLO_PASSWORD=your_password_encoded
AROFLO_SECRET_KEY=your_secret_key
AROFLO_HOST_IP=
```

Optionally set a primary client name for revenue segmentation:

```
PRIMARY_CLIENT=Your Main Client Name
```

### 3. Test the connection

```bash
python main.py test
```

## Usage

### Test API Connection

```bash
python main.py test
```

### Generate Monthly Report

```bash
python main.py report                          # Current month
python main.py report --month 1 --year 2026    # Specific month
```

### Proofread Job Cards

```bash
python main.py proofread                # Show only jobs with errors
python main.py proofread --show-all     # Show all jobs including clean ones
```

### Mark Jobs Ready to Invoice

```bash
python main.py mark-ready               # Preview (dry run)
python main.py mark-ready --apply       # Actually update tasks
```

### Fetch Monthly Metrics

```bash
python main.py update                          # Current month
python main.py update --month 1 --year 2026    # Specific month
```

### Combined Workflow: Proofread, Fix, and Mark Ready

The recommended end-of-period workflow runs everything in one step:

```bash
python proofread_and_mark_ready.py          # Dry run - preview changes
python proofread_and_mark_ready.py --apply  # Apply corrections and mark ready
```

This fetches all completed tasks, proofreads descriptions and timesheet notes, writes description corrections back to AroFlo, prints a manual corrections list for any timesheet note errors, and marks everything as Ready to Invoice.

## Project structure

| File | Purpose |
|------|---------|
| `aroflo_connector.py` | API auth (HMAC-SHA512), GET/POST, rate limiting (120/min, 3/sec burst) |
| `config.py` | Environment loading; secrets can live outside the project folder |
| `bill_review.py` | Nightly bill check (rules + PDF verification + Telegram summary) |
| `job_monitor.py` / `database.py` | Completed-job polling with SQLite tracking |
| `proofreader.py` | LanguageTool (en-AU) + trade dictionary + custom corrections |
| `proofread_and_mark_ready.py` / `mark_ready_to_invoice.py` | End-of-period workflow |
| `po_report.py` | PO approval backlog |
| `materials_recon.py` / `stocktake.py` | Stock balance and charge-out check |
| `data_extractor.py` / `spreadsheet_updater.py` / `main.py` | Monthly metrics and scorecard |
| `tests/` | Offline unit tests |

## Environment variables

See [`.env.example`](.env.example). AroFlo credentials are required. `PRIMARY_CLIENT`, `LABOUR_ONLY_CLIENTS`, `MARKUP_TARGET`, `SCORECARD_PATH` and the Telegram settings are optional.

## License

[MIT](LICENSE)
