"""Make the repo root importable and keep tests offline (no real credentials)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Fake credentials so config.py loads cleanly. Nothing here ever reaches AroFlo:
# every test exercises pure logic or mocks the network.
for key, value in {
    "AROFLO_ORG_NAME": "org-test",
    "AROFLO_USERNAME": "user-test",
    "AROFLO_PASSWORD": "pass-test",
    "AROFLO_SECRET_KEY": "secret-test",
    "PRIMARY_CLIENT": "Acme Facilities",
}.items():
    os.environ.setdefault(key, value)
