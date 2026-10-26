"""Pytest isolation: the unit test suite must NEVER touch real Apify / Airtable.

config.py reads credentials from the environment (and .env via python-dotenv) at import time.
Because python-dotenv does not override variables that are already set, blanking them here
(before any test module imports `config`) guarantees the suite is offline and write-safe,
even when a developer's shell or .env contains live keys / live_ingestion mode.
"""

import os

os.environ["APIFY_API_TOKEN"] = ""
os.environ["AIRTABLE_API_KEY"] = ""
os.environ["EXECUTION_MODE"] = "dry_run"
os.environ["ENABLE_LIVE_INGESTION"] = "false"