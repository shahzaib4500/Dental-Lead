"""Central configuration module for dental-lead-generator.

Reads runtime and credential settings from environment variables.
Business rules, territories, and blockers are maintained in config/*.json.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Dict, Optional
from dotenv import load_dotenv

# Automatically load .env if present
load_dotenv()


class ExecutionMode(str, Enum):
    """Safe execution modes."""
    DRY_RUN = "dry_run"
    LIVE_DISCOVERY = "live_discovery"
    LIVE_INGESTION = "live_ingestion"


class CampaignType(str, Enum):
    """Lead campaign types."""
    PRM = "PRM"  # General, family, comprehensive dentistry
    WISDOM_SALES = "WISDOM_SALES"  # Dental specialists


class QualificationStatus(str, Enum):
    """Standardized qualification statuses mapped to Airtable options."""
    QUALIFIED = "Qualified"
    DISQUALIFIED = "Disqualified"
    NEEDS_REVIEW = "Needs Review"
    DUPLICATE = "Duplicate"
    EXISTING_REFERRER_OR_DNC = "Existing Referrer / DNC"


class DuplicateStatus(str, Enum):
    """Duplicate status flags."""
    UNIQUE = "Unique"
    POTENTIAL_DUPLICATE = "Potential Duplicate"
    CONFIRMED_DUPLICATE = "Confirmed Duplicate"
    DIFFERENT_LOCATION = "Different Location of Known Entity"


class Config:
    """Runtime credentials and system settings container."""

    # Apify Google Maps Actor (nwua9Gu5YrADL7ZDj - compass/crawler-google-places)
    APIFY_API_TOKEN: str = os.getenv("APIFY_API_TOKEN", "")
    APIFY_ACTOR_ID: str = os.getenv("APIFY_ACTOR_ID", "nwua9Gu5YrADL7ZDj")
    APIFY_API_BASE: str = os.getenv("APIFY_API_BASE", "https://api.apify.com/v2")
    APIFY_MAX_CRAWLED_PLACES_PER_SEARCH: int = int(os.getenv("MAX_RESULTS_PER_SEARCH", os.getenv("APIFY_MAX_CRAWLED_PLACES_PER_SEARCH", "20")))
    APIFY_RUN_TIMEOUT: int = int(os.getenv("APIFY_RUN_TIMEOUT", "300"))
    APIFY_LANGUAGE: str = os.getenv("APIFY_LANGUAGE", "en")
    APIFY_SKIP_CLOSED_PLACES: bool = os.getenv("APIFY_SKIP_CLOSED_PLACES", "true").lower() in ("true", "1", "yes")

    # Airtable Target Base & Tables
    AIRTABLE_API_KEY: str = os.getenv("AIRTABLE_API_KEY", "")
    AIRTABLE_BASE_ID: str = os.getenv("AIRTABLE_BASE_ID", "appnKumhO07VlzUe8")
    AIRTABLE_TABLE_NAME: str = os.getenv("AIRTABLE_TABLE_NAME", "tblLzZp1Gc0Pvyvo5")  # Practice Pipeline
    AIRTABLE_ZIPS_TABLE_NAME: str = os.getenv("AIRTABLE_ZIPS_TABLE_NAME", "tblu1CY9070zVsqot")  # Zipcode List
    AIRTABLE_DNC_TABLE_NAME: str = os.getenv("AIRTABLE_DNC_TABLE_NAME", "tblHhplF40Xc8FYQ7")    # CLEAN DNC List

    # Safe operating mode
    EXECUTION_MODE: ExecutionMode = ExecutionMode(os.getenv("EXECUTION_MODE", ExecutionMode.DRY_RUN.value).lower())
    ENABLE_LIVE_INGESTION: bool = os.getenv("ENABLE_LIVE_INGESTION", "false").lower() in ("true", "1", "yes")

    # Airtable Field Mapping (Internal record key -> Airtable column name or ID)
    AIRTABLE_FIELD_MAPPING: Dict[str, str] = {
        "practice_name": os.getenv("AIRTABLE_FIELD_PRACTICE_NAME", "fldJdczwAbnLj0Xbo"),  # Practice Name
        "phone": os.getenv("AIRTABLE_FIELD_PHONE", "fldBcsn0PKsTHv5H8"),                  # Phone Number
        "website": os.getenv("AIRTABLE_FIELD_WEBSITE", "fldv9w9larKPIvehO"),              # Company Website
        "address": os.getenv("AIRTABLE_FIELD_ADDRESS", "fld2oHbwyUU5zrCpX"),              # Address
        "zip_link": os.getenv("AIRTABLE_FIELD_ZIP_LINK", "fldQTixIhZnJui3VE"),            # Zipcode List
        "zip_text": os.getenv("AIRTABLE_FIELD_ZIP_TEXT", "fld4luux8OT8Y7nzx"),            # Zipcode
        "practice_type": os.getenv("AIRTABLE_FIELD_PRACTICE_TYPE", "fldC5eQbXfAU5Mpz4"),  # Practice Type
        "lead_type": os.getenv("AIRTABLE_FIELD_LEAD_TYPE", "fldChHcXBg1GNId3S"),          # Lead Type
        "qualification": os.getenv("AIRTABLE_FIELD_QUALIFIED", "fldhFjad7nSaIrMS0"),      # Qualified?
        "prospect_date": os.getenv("AIRTABLE_FIELD_PROSPECT_DATE", "fldwTfzCgPSf0StPp"),  # Prospect Date
        "prospect_log": os.getenv("AIRTABLE_FIELD_PROSPECT_LOG", "fldaQf0VFBZ9PynmK"),    # Prospect Log
    }

    @classmethod
    def is_live_ingestion_authorized(cls) -> bool:
        """Strict check to ensure live ingestion is intentionally enabled and configured."""
        return (
            cls.EXECUTION_MODE == ExecutionMode.LIVE_INGESTION
            and cls.ENABLE_LIVE_INGESTION is True
            and bool(cls.AIRTABLE_API_KEY)
            and bool(cls.AIRTABLE_BASE_ID)
            and bool(cls.AIRTABLE_TABLE_NAME)
        )
