"""
AroFlo API Configuration

This file contains configuration settings for the AroFlo API integration.
Credentials should be stored in environment variables or a .env file.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

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

# Host IP - your public IP address (optional, but may be required)
# Set to empty string to disable
AROFLO_HOST_IP = os.getenv("AROFLO_HOST_IP", "").strip()

# Rate limiting (from official AroFlo API docs)
API_DAILY_LIMIT = 2000
API_CALLS_PER_MINUTE = 120  # Official limit is 120/min, with max 3/sec burst

# Primary client name - used for client segmentation in reporting
# Set this to your main/anchor client name for revenue breakdown metrics
PRIMARY_CLIENT = os.getenv("PRIMARY_CLIENT", "").strip()
