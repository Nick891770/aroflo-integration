# AroFlo Integration Suite

Automated proofreading, reporting, and invoice-readiness checking for AroFlo job management.

## The Problem

[AroFlo](https://www.aroflo.com/) is widely used by trade businesses in Australia for job management, but it lacks built-in proofreading for job notes, automated financial reporting, and bulk status updates. Job cards go to invoicing with typos in the work descriptions, monthly reporting requires manual data extraction, and marking jobs as ready-to-invoice is a repetitive click-fest through every completed task.

## The Solution

A Python suite that connects to the AroFlo API to:

1. **Proofread all completed job cards** using the LanguageTool API with a custom trade-specific dictionary -- catches spelling mistakes, grammar errors, and common shorthand misspellings before jobs are invoiced
2. **Auto-correct task descriptions** directly in AroFlo via the API, and generate a manual corrections list for timesheet notes (the AroFlo API does not support updating timesheet notes -- see [Known Limitations](#known-limitations))
3. **Extract monthly financial metrics** including revenue, profit margins, job counts, and client segmentation breakdowns
4. **Bulk-mark completed jobs** as "Ready to Invoice" in a single command instead of clicking through each one

## Key Features

- **Australian English (en-AU) support** -- uses LanguageTool's Australian English ruleset so "colour" and "realise" are not flagged
- **Trade term dictionary** -- whitelists electrical terms (GPO, RCD, MCB, RCBO, submain, busbar, switchgear) so they are not flagged as misspellings
- **Custom corrections for trade shorthand** -- automatically fixes common sparkie misspellings like "did'nt", "wasnt", "outler" (outlet), "conection", "andi" (and I)
- **Prevented bad corrections** -- "power point" will not become "PowerPoint", "Haas" (CNC brand) will not become "has", plural "circuits" will not become possessive "circuit's"
- **LanguageTool API with fallback** -- uses the free LanguageTool public API (no Java required), falls back to pyspellchecker for offline use
- **HMAC-SHA512 API authentication** -- implements AroFlo's Postman-style HMAC signing with correct payload construction
- **Rate limiting with exponential backoff** -- respects AroFlo's 120 req/min and 3 req/sec limits, retries on transient failures
- **Client segmentation** -- configurable primary client tracking for revenue concentration analysis
- **Dry-run mode** -- every destructive command defaults to preview mode, requiring `--apply` to make changes

## Built With

- **Python** -- core language
- **AroFlo API** -- HMAC-SHA512 authenticated REST API for job management data
- **LanguageTool API** -- grammar and spelling checking with Australian English support
- **openpyxl** -- Excel spreadsheet reading/writing for scorecard updates
- **pyspellchecker** -- offline fallback spell checker with custom word lists

## Context

Built for an electrical contracting business in Brisbane, Australia, to automate quality control and reporting workflows. The proofreader's trade term dictionary and custom corrections are tuned for electrical trade job descriptions but can be adapted for any trade.

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

## Project Structure

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point with subcommands |
| `aroflo_connector.py` | API authentication (HMAC-SHA512), GET/POST requests, rate limiting |
| `data_extractor.py` | Fetches invoices, calculates revenue/profit/client metrics |
| `proofreader.py` | Spelling/grammar checking with trade term dictionary |
| `spreadsheet_updater.py` | Updates Excel scorecard with monthly metrics |
| `proofread_and_mark_ready.py` | Combined workflow: proofread + fix + mark ready |
| `mark_ready_to_invoice.py` | Standalone: bulk-mark tasks as Ready to Invoice |
| `config.py` | Configuration and environment variable loading |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AROFLO_ORG_NAME` | orgEncoded value from AroFlo admin |
| `AROFLO_USERNAME` | uEncoded value from AroFlo admin |
| `AROFLO_PASSWORD` | pEncoded value (API token) from AroFlo admin |
| `AROFLO_SECRET_KEY` | Secret key for HMAC-SHA512 authentication |
| `AROFLO_HOST_IP` | Your public IP address (optional, leave empty to disable) |
| `PRIMARY_CLIENT` | Primary client name for revenue segmentation (optional) |
| `SCORECARD_PATH` | Path to scorecard spreadsheet (optional, defaults to `scorecard.xlsx`) |

## Known Limitations

- **Timesheet notes are read-only via the AroFlo API.** The API accepts update requests for timesheet notes and returns a success response (`updatetotal:1`), but silently ignores the changes. Task descriptions and substatuses *can* be updated. As a workaround, the proofreader prints a manual corrections list for any timesheet note errors so you can copy-paste fixes in the AroFlo UI.

## License

This project is provided as a portfolio demonstration. See individual file headers for details.
