"""
AroFlo API Configuration

This file contains configuration settings for the AroFlo API integration.
Credentials should be stored in environment variables or a .env file.
"""

import os

import pathlib

# Secrets can live outside the project folder (e.g. one that syncs to cloud storage):
# ~/.secrets/aroflo.env wins when it exists; the local .env below is the fallback.
# Nothing overrides an already-set environment variable.
_secrets = pathlib.Path.home() / ".secrets" / "aroflo.env"
if _secrets.exists():
    for _line in _secrets.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Load .env. Prefer python-dotenv, but fall back to a tiny parser so the tool
# runs on hosts without dotenv installed (e.g. the server under PEP 668).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    import pathlib
    _envpath = pathlib.Path(__file__).with_name(".env")
    if _envpath.exists():
        for _line in _envpath.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# AroFlo API Configuration
AROFLO_BASE_URL = "https://api.aroflo.com/"

# Credentials - these should be set as environment variables
# DO NOT hardcode actual credentials here
# These are the pre-encoded values from AroFlo Site Admin > Settings > General > AroFlo API
# Note: .strip() removes any accidental whitespace/newlines from copy-paste
AROFLO_ORG_NAME = os.getenv("AROFLO_ORG_NAME", "").strip()      # orgEncoded
AROFLO_USERNAME = os.getenv("AROFLO_USERNAME", "").strip()      # uEncoded
AROFLO_PASSWORD = os.getenv("AROFLO_PASSWORD", "").strip()      # pEncoded
AROFLO_SECRET_KEY = os.getenv("AROFLO_SECRET_KEY", "").strip()  # Secret Key

# Primary client name - used for client segmentation in reporting.
# Set this to your main/anchor client for revenue-concentration metrics.
PRIMARY_CLIENT = os.getenv("PRIMARY_CLIENT", "").strip()

# Host IP - your public IP address (optional, but may be required)
# Set to empty string to disable
AROFLO_HOST_IP = os.getenv("AROFLO_HOST_IP", "").strip()



# Telegram (for the nightly bill-review summary)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Rate limiting (from official AroFlo API docs)
API_DAILY_LIMIT = 2000
API_CALLS_PER_MINUTE = 120  # Official limit is 120/min, with max 3/sec burst

# Spreadsheet configuration
SPREADSHEET_PATH = os.getenv("SCORECARD_PATH", "").strip() or os.path.join(os.path.dirname(__file__), "scorecard.xlsx")

# Month column mapping (1-indexed columns in Excel)
# Each month has Target and Actual sub-columns
# Based on spreadsheet inspection: Nov starts at col D (4)
MONTH_COLUMNS = {
    "NOVEMBER": {"target": "D", "actual": "E"},   # Cols 4-5
    "DECEMBER": {"target": "F", "actual": "G"},   # Cols 6-7
    "JANUARY": {"target": "H", "actual": "I"},    # Cols 8-9
    "FEBRUARY": {"target": "J", "actual": "K"},   # Cols 10-11
    "MARCH": {"target": "L", "actual": "M"},      # Cols 12-13
    "APRIL": {"target": "N", "actual": "O"},      # Cols 14-15
    "MAY": {"target": "P", "actual": "Q"},        # Cols 16-17
    "JUNE": {"target": "R", "actual": "S"},       # Cols 18-19
    "JULY": {"target": "T", "actual": "U"},       # Cols 20-21
    "AUGUST": {"target": "V", "actual": "W"},     # Cols 22-23
    "SEPTEMBER": {"target": "X", "actual": "Y"},  # Cols 24-25
    "OCTOBER": {"target": "Z", "actual": "AA"},   # Cols 26-27
}

# Row numbers for metrics (based on spreadsheet inspection)
# LAGGING/PAST section (starts at row 6)
LAGGING_ROWS = {
    "revenue": 8,               # Revenue/Sales Income
    "gross_profit_dollars": 10, # Gross Profit $
    "net_profit_dollars": 12,   # Net Profit $
    "gross_profit_percent": 14, # Gross Profit % (FORMULA - don't overwrite)
    "net_profit_percent": 16,   # Net Profit % (FORMULA - don't overwrite)
    "completed_jobs": 18,       # Number of completed jobs
    "average_job_value": 20,    # Average job value
}

# LEADING/PREDICTIVE section (starts at row 22)
LEADING_ROWS = {
    "primary_client_jobs": 24,           # Primary Client - # jobs
    "primary_client_value": 26,          # Primary Client - $ value
    "other_client_jobs": 28,   # Other Clients - # jobs
    "other_client_value": 30,  # Other Clients - $ value
    "jobs_total": 32,           # Jobs Total (FORMULA - don't overwrite)
    "primary_client_percent": 34,        # % Of Sales from primary client work (FORMULA)
    "other_client_percent": 36,      # % Of Sales from outside work (FORMULA)
}
