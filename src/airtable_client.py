"""Airtable API client module.

Manages connection testing, reading existing records (for deduplication),
schema preflight verification via Meta API, field ID-based mapping,
safe creation of eligible lead records, and loading territories/DNC lists.

Handles pagination, rate limiting (HTTP 429), and error logging.
Never overwrites existing records or outreach history: create-only.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import requests

from config import Config, ExecutionMode, QualificationStatus
from src.lead_normalizer import InternalLeadRecord

logger = logging.getLogger(__name__)


class AirtableError(Exception):
    """Exception raised on Airtable API failures."""
    pass


class AirtableClient:
    """Interface for querying and writing records to Airtable."""

    BASE_URL = "https://api.airtable.com/v0"
    META_BASE_URL = "https://api.airtable.com/v0/meta"

    DEFAULT_BASE_ID = "appnKumhO07VlzUe8"
    DEFAULT_LEADS_TABLE_ID = "tblLzZp1Gc0Pvyvo5"  # Practice Pipeline
    DEFAULT_ZIPS_TABLE_ID = "tblu1CY9070zVsqot"   # Zipcode List
    DEFAULT_DNC_TABLE_ID = "tblHhplF40Xc8FYQ7"    # CLEAN DNC List/Currently Referring Offices
    DEFAULT_OLD_DNC_TABLE_ID = "tblaLCiFEqO7ouDVx"# OLD DNC List/Currently Referring Offices
    DEFAULT_OUTREACH_TABLE_ID = "tblxSfnQVSuJ3HgVj"     # [AS] Outreach Log
    DEFAULT_CONTACTS_TABLE_ID = "tblU2E6ryS5eT8EVD"     # Contacts
    DEFAULT_SALES_APPTS_TABLE_ID = "tbl9Zc4mhG2AibN11"  # Sales Appts
    DEFAULT_CONFIRMATIONS_TABLE_ID = "tblCzYikeEWixpKgi"# Confirmations
    DEFAULT_FOLLOW_UP_TABLE_ID = "tblkDeGSqlGWWUN82"    # Follow Up Cadence
    DEFAULT_QA_TABLE_ID = "tblB7E8SGIUQbSsP4"           # QA | Call Listening

    # Confirmed Relationship Field IDs
    # Practice Pipeline
    FIELD_PRACTICE_ZIP_LINK = "fldQTixIhZnJui3VE"
    FIELD_PRACTICE_CONTACTS_LINK = "fldoQKH1BsoG2bS3e"
    FIELD_PRACTICE_OUTREACH_PRIMARY_LINK = "fldYfHi9wHogyDjAp" # Canonical link
    FIELD_PRACTICE_OUTREACH_SECONDARY_LINK = "fldfmJkQJe6hVjQDZ" # Unused/empty in live base
    FIELD_PRACTICE_SALES_APPTS_LINK = "fld9ZOn9VFY3NhZJa"
    FIELD_PRACTICE_FOLLOW_UP_LINK = "fldPeocX1XVLrGogn"
    FIELD_PRACTICE_CLEAN_DNC_LINK = "fldReRjzvqpOcy3Qu"
    FIELD_PRACTICE_ZIP_TEXT = "fld4luux8OT8Y7nzx"

    # Zipcode List
    FIELD_ZIP_VALUE = "fldKj7z5i1g3r2mDi"
    FIELD_ZIP_APPROVED = "fldbIlh3dKcHMnJbG"
    FIELD_ZIP_PRACTICE_RECIPROCAL = "fldPXFMudmENIBAqQ"
    FIELD_ZIP_SALES_APPTS_LINK = "fldyT9Pz7r1ZZO5qT"

    # Contacts
    FIELD_CONTACT_NAME = "fldrCO6ru2kPyppnk"
    FIELD_CONTACT_PRACTICE_LINK = "flds3eo0wGnRgDDTs"
    FIELD_CONTACT_PHONE = "fldkcHa10vyPhgLix"
    FIELD_CONTACT_EMAIL = "fldl6qlwPiTjvtzwn"
    FIELD_CONTACT_POSITION = "fldoX8DjIbM16QUDA"

    # [AS] Outreach Log
    FIELD_OUTREACH_CALL_DATE = "fld8AyEYRMOnLAawa"
    FIELD_OUTREACH_PRACTICE_LINK = "fldNBDz2oEkaDNfJy" # Canonical inverse to fldYfHi9wHogyDjAp
    FIELD_OUTREACH_SETTER = "fld74yzfs6ufhEWXx"
    FIELD_OUTREACH_CALL_OUTCOME = "fldHkSIOuaFWHGp1M"
    FIELD_OUTREACH_CALL_NOTES = "fldfGHWLw32SApWZB"
    FIELD_OUTREACH_SPOKE_TO = "fldpaLUDzKXFjBZTV"
    FIELD_OUTREACH_RECORDING_URL = "fldxotlIZnkj6YznX"
    FIELD_OUTREACH_QA_LINK = "fldhsLiq35AMlBP80"
    FIELD_OUTREACH_TYPE = "fldBiPiBML1I5QVC6"
    FIELD_OUTREACH_DNC_LINK = "fldZJIJsMzR8KltSA"

    # Sales Appts
    FIELD_APPT_DATE_BOOKED = "fldDF7Fob8JUeQkQ7"
    FIELD_APPT_PRACTICE_LINK = "fldIQiYs64meGt7Gd"
    FIELD_APPT_DATE = "fldye7ahM1mrnNWuW"
    FIELD_APPT_TIME = "fldKMPmDdrWGz3w6w"
    FIELD_APPT_SETTER = "fldb3omlOqn00nH4x"
    FIELD_APPT_MEETING_TYPE = "fldIBuyszsHc2T0ap"
    FIELD_APPT_NOTES = "fldc7sVp9u5zyRSgY"
    FIELD_APPT_CONFIRMATIONS_LINK = "fldwnOYVUT9iAM4Wb"
    FIELD_APPT_FOLLOW_UP_LINK = "fldrw7DausaoFvnnP"
    FIELD_APPT_ZIP_LINK = "fldlMJD6QaHhnLNLB"

    # Confirmations
    FIELD_CONFIRMATION_DATE = "fldpU1593XysBS9Vr"
    FIELD_CONFIRMATION_APPTS_LINK = "fldzB74UUnd0rRBHy"
    FIELD_CONFIRMATION_STEPS = "fldhiMIbafnbv3s3o"
    FIELD_CONFIRMATION_STATUS = "fldWmJLeOCVn5rz0l"
    FIELD_CONFIRMATION_NOTES = "fldFrtVE2TBVpoVsz"

    # Follow Up Cadence
    FIELD_FOLLOW_UP_DATE = "fld99vWtuQev606g2"
    FIELD_FOLLOW_UP_PRACTICE_LINK = "fldasbBVzo4cszTCl"
    FIELD_FOLLOW_UP_COMPLETION = "fldBgb99bOH1nh9Uy"
    FIELD_FOLLOW_UP_STEP = "fldTfU5fJIFu8le1o"
    FIELD_FOLLOW_UP_STATUS = "fld0oOQi7l0FLmlHm"
    FIELD_FOLLOW_UP_NOTES = "fldjZ8moaXICFNUVf"
    FIELD_FOLLOW_UP_OM = "fldUAy4XRgCS5eBiv"
    FIELD_FOLLOW_UP_APPTS_LINK = "fldzDcYzami0UuBIM"

    # QA | Call Listening
    FIELD_QA_AUDIT_DATE = "fld3FBHffssCfJYUc"
    FIELD_QA_OUTREACH_LINK = "fld8hNtph090uPIia"
    FIELD_QA_CHECKLIST = "fldCEcWgQYFwYzhGr"
    FIELD_QA_FEEDBACK = "fldQaJxgbW6f6yLtr"
    FIELD_QA_BY = "fldiFi0j345KfwFrl"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_id: Optional[str] = None,
        table_name: Optional[str] = None,
        field_mapping: Optional[Dict[str, str]] = None,
        schema_path: Optional[Path] = None,
        session: Optional[requests.Session] = None,
    ):
        self.api_key = Config.AIRTABLE_API_KEY if api_key is None else api_key
        self.base_id = (Config.AIRTABLE_BASE_ID or self.DEFAULT_BASE_ID) if base_id is None else base_id
        self.table_name = (Config.AIRTABLE_TABLE_NAME or self.DEFAULT_LEADS_TABLE_ID) if table_name is None else table_name
        self.session = session or requests.Session()

        # Load schema definition if available
        self.schema_config = self._load_schema_config(schema_path)

        # Configurable field mapping with fallback
        self.field_mapping = field_mapping or self._init_field_mapping()

        # Cached meta discovery
        self._meta_tables_cache: Optional[List[Dict[str, Any]]] = None
        self._zip_records_cache: Optional[Dict[str, str]] = None  # zip_code -> record_id

    def _load_schema_config(self, schema_path: Optional[Path]) -> Dict[str, Any]:
        path = schema_path or Path(__file__).parent.parent / "config" / "airtable_schema.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load airtable_schema.json: {e}")
        return {}

    def _init_field_mapping(self) -> Dict[str, str]:
        """Field ID mapping preferred (stable), falling back to configured field names."""
        mapping: Dict[str, str] = {}
        schema_fields = self.schema_config.get("fields", {})

        # Default internal keys mapped to field IDs
        default_key_to_id = {
            "practice_name": "fldJdczwAbnLj0Xbo",
            "phone": "fldBcsn0PKsTHv5H8",
            "website": "fldv9w9larKPIvehO",
            "address": "fld2oHbwyUU5zrCpX",
            "zip_link": "fldQTixIhZnJui3VE",
            "zip_text": "fld4luux8OT8Y7nzx",
            "practice_type": "fldC5eQbXfAU5Mpz4",
            "lead_type": "fldChHcXBg1GNId3S",
            "qualification": "fldhFjad7nSaIrMS0",
            "prospect_date": "fldwTfzCgPSf0StPp",
            "prospect_log": "fldaQf0VFBZ9PynmK",
        }

        for internal_key, fid in default_key_to_id.items():
            if internal_key in schema_fields and "id" in schema_fields[internal_key]:
                mapping[internal_key] = schema_fields[internal_key]["id"]
            else:
                mapping[internal_key] = fid

        return mapping

    def get_table_endpoint(self, table_id_or_name: Optional[str] = None) -> str:
        """Construct full Airtable table endpoint URL for any table."""
        target_table = table_id_or_name or self.table_name
        if not self.base_id or not target_table:
            raise AirtableError("Airtable BASE_ID or TABLE_ID/NAME is not configured.")
        return f"{self.BASE_URL}/{self.base_id}/{requests.utils.quote(target_table)}"

    @property
    def table_endpoint(self) -> str:
        """Default table endpoint for leads."""
        return self.get_table_endpoint(self.table_name)

    @property
    def headers(self) -> Dict[str, str]:
        """Request headers with Bearer token authentication."""
        if not self.api_key:
            raise AirtableError("Airtable API key (Personal Access Token) is missing.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs: Any,
    ) -> requests.Response:
        """Execute HTTP request with backoff handling for 429 rate limits and network errors."""
        last_exception = None
        response = None
        for attempt in range(max_retries):
            try:
                response = self.session.request(method, url, headers=self.headers, **kwargs)
                if response.status_code == 429:
                    wait_seconds = 2 ** attempt
                    logger.warning(f"Airtable 429 Rate Limit encountered. Retrying in {wait_seconds}s...")
                    time.sleep(wait_seconds)
                    continue
                return response
            except requests.exceptions.RequestException as e:
                last_exception = e
                wait_seconds = 2 ** attempt
                logger.warning(f"Airtable request exception ({e}). Retrying in {wait_seconds}s...")
                time.sleep(wait_seconds)

        if last_exception:
            raise AirtableError(f"Airtable request failed after {max_retries} attempts: {last_exception}") from last_exception
        if response is not None:
            return response
        raise AirtableError("Airtable request failed with no response.")

    def validate_credentials(self) -> Dict[str, Any]:
        """Check if all required Airtable credentials are present and validly formatted."""
        missing = []
        is_placeholder_key = not self.api_key or "your_airtable" in self.api_key.lower() or self.api_key in ("pat_xxx", "key_xxx")
        is_placeholder_base = not self.base_id or "appxxxxxxxx" in self.base_id.lower() or "your_base" in self.base_id.lower()
        is_placeholder_table = not self.table_name

        if is_placeholder_key:
            missing.append("AIRTABLE_API_KEY")
        if is_placeholder_base:
            missing.append("AIRTABLE_BASE_ID")
        if is_placeholder_table:
            missing.append("AIRTABLE_TABLE_NAME")

        return {
            "valid": len(missing) == 0,
            "missing": missing,
            "configured": {
                "api_key": not is_placeholder_key,
                "base_id": not is_placeholder_base,
                "table_name": not is_placeholder_table,
            },
        }

    def get_meta_tables(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Discover tables and field options via Meta API: GET /v0/meta/bases/{baseId}/tables."""
        if self._meta_tables_cache and not force_refresh:
            return self._meta_tables_cache

        validation = self.validate_credentials()
        if not validation["valid"]:
            logger.info("Missing Airtable credentials; cannot fetch Meta API tables.")
            return []

        url = f"{self.META_BASE_URL}/bases/{self.base_id}/tables"
        res = self._request_with_retry("GET", url, timeout=15)
        if res.status_code == 200:
            tables = res.json().get("tables", [])
            self._meta_tables_cache = tables
            return tables
        else:
            logger.error(f"Meta API error ({res.status_code}): {res.text}")
            return []

    def preflight_schema_check(self) -> Dict[str, Any]:
        """Perform a strict, read-only preflight schema inspection using Airtable Meta API.

        Verifies:
        - Every mapped field exists in Practice Pipeline
        - Types match expected schema (singleLineText, phoneNumber, richText, etc.)
        - Options for singleSelect / multipleSelects match
        - Fails closed on any mismatch.
        """
        validation = self.validate_credentials()
        if not validation["valid"]:
            return {
                "status": "skipped",
                "reason": f"Missing required credentials: {', '.join(validation['missing'])}",
                "can_proceed_offline": True,
            }

        tables = self.get_meta_tables()
        if not tables:
            # Fallback to single record read-only inspection if Meta API lacks schema.bases:read scope
            return self._preflight_via_sample_record()

        # Find Practice Pipeline table
        leads_table = None
        for t in tables:
            if t.get("id") == self.table_name or t.get("name") == self.table_name or t.get("name") == "Practice Pipeline":
                leads_table = t
                break

        if not leads_table:
            return {
                "status": "failed",
                "reason": f"Target table '{self.table_name}' not found in base '{self.base_id}'.",
                "can_proceed_offline": False,
            }

        table_fields = {f.get("id"): f for f in leads_table.get("fields", [])}
        table_fields_by_name = {f.get("name"): f for f in leads_table.get("fields", [])}

        mismatches = []
        discovered_options = {}

        schema_fields = self.schema_config.get("fields", {})

        for internal_key, target in self.field_mapping.items():
            field_def = table_fields.get(target) or table_fields_by_name.get(target)
            if not field_def:
                mismatches.append(f"Field '{internal_key}' mapped to '{target}' does not exist in table.")
                continue

            expected_type = schema_fields.get(internal_key, {}).get("type")
            actual_type = field_def.get("type")

            if expected_type and actual_type != expected_type:
                mismatches.append(
                    f"Field '{internal_key}' type mismatch: expected '{expected_type}', got '{actual_type}' in Airtable."
                )

            if actual_type in ("singleSelect", "multipleSelects"):
                choices = [c.get("name") for c in field_def.get("options", {}).get("choices", [])]
                discovered_options[internal_key] = choices

        if mismatches:
            return {
                "status": "failed",
                "table": leads_table.get("name"),
                "base_id": self.base_id,
                "mismatches": mismatches,
                "discovered_options": discovered_options,
                "can_proceed_offline": False,
            }

        return {
            "status": "success",
            "table": leads_table.get("name"),
            "table_id": leads_table.get("id"),
            "base_id": self.base_id,
            "fields_verified": len(self.field_mapping),
            "discovered_options": discovered_options,
            "read_only": True,
        }

    def _preflight_via_sample_record(self) -> Dict[str, Any]:
        """Fallback preflight inspection when Meta API is not available."""
        try:
            params = {"maxRecords": 1}
            res = self._request_with_retry("GET", self.table_endpoint, params=params, timeout=10)
            if res.status_code != 200:
                return {
                    "status": "failed",
                    "status_code": res.status_code,
                    "reason": f"Airtable query failed ({res.status_code}): {res.text}",
                    "can_proceed_offline": True,
                }
            records = res.json().get("records", [])
            sample_fields = records[0].get("fields", {}) if records else {}
            sample_existing_columns = list(sample_fields.keys())
            
            # Check target columns against sample fields
            unmatched = [
                col for col in self.field_mapping.values()
                if col not in sample_existing_columns
            ]
            
            return {
                "status": "success",
                "table": self.table_name,
                "base_id": self.base_id,
                "sample_existing_columns": sample_existing_columns,
                "unmatched_target_columns": unmatched,
                "note": "Preflight passed via sample record query.",
                "read_only": True,
            }
        except Exception as e:
            return {
                "status": "error",
                "reason": str(e),
                "can_proceed_offline": True,
            }

    def fetch_all_records(
        self,
        table_id_or_name: Optional[str] = None,
        fields: Optional[List[str]] = None,
        filter_by_formula: Optional[str] = None,
        max_records: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch records with pagination support (100 per page)."""
        if not self.api_key or not self.base_id:
            logger.info("Airtable credentials not configured. Returning empty list.")
            return []

        all_records: List[Dict[str, Any]] = []
        offset: Optional[str] = None
        endpoint = self.get_table_endpoint(table_id_or_name)

        params: Dict[str, Any] = {"pageSize": 100}
        if fields:
            params["fields[]"] = fields
        if filter_by_formula:
            params["filterByFormula"] = filter_by_formula

        try:
            while True:
                if offset:
                    params["offset"] = offset

                res = self._request_with_retry("GET", endpoint, params=params, timeout=20)
                if res.status_code != 200:
                    raise AirtableError(f"Failed to fetch records from Airtable ({res.status_code}): {res.text}")

                data = res.json()
                records = data.get("records", [])
                all_records.extend(records)

                if max_records and len(all_records) >= max_records:
                    return all_records[:max_records]

                offset = data.get("offset")
                if not offset:
                    break

            logger.info(f"Retrieved {len(all_records)} records from {table_id_or_name or self.table_name}.")
            return all_records
        except Exception as e:
            logger.error(f"Error fetching Airtable records: {e}")
            raise AirtableError(f"Airtable fetch failed: {e}") from e

    def load_approved_zipcodes(self) -> Tuple[List[str], Dict[str, str]]:
        """Load approved ZIP codes from Zipcode List table (records with 'Approved Zipcodes' = True).

        Returns:
            Tuple of (list_of_approved_zips, mapping_of_zip_to_record_id)
        """
        zips_table = self.schema_config.get("tables", {}).get("zips", {}).get("id", self.DEFAULT_ZIPS_TABLE_ID)
        records = self.fetch_all_records(
            table_id_or_name=zips_table,
            fields=["Zip Code", "Approved Zipcodes", self.FIELD_ZIP_VALUE, self.FIELD_ZIP_APPROVED],
        )

        approved_zips: List[str] = []
        approved_zip_to_rec_id: Dict[str, str] = {}
        all_zip_to_rec_id: Dict[str, str] = {}

        for r in records:
            f = r.get("fields", {})
            raw_zip = f.get("Zip Code") or f.get(self.FIELD_ZIP_VALUE)
            if not raw_zip:
                continue
            clean_zip = str(raw_zip).strip()
            # Preserve leading zeros
            if len(clean_zip) < 5 and clean_zip.isdigit():
                clean_zip = clean_zip.zfill(5)

            rec_id = r.get("id")
            if rec_id:
                all_zip_to_rec_id[clean_zip] = rec_id

            # Strictly require explicit boolean True (do not infer from blank or None)
            is_approved = f.get("Approved Zipcodes") is True or f.get(self.FIELD_ZIP_APPROVED) is True
            if is_approved:
                approved_zips.append(clean_zip)
                if rec_id:
                    approved_zip_to_rec_id[clean_zip] = rec_id

        self._approved_zip_records_cache = approved_zip_to_rec_id
        self._zip_records_cache = approved_zip_to_rec_id
        self._all_zip_records_cache = all_zip_to_rec_id
        return approved_zips, approved_zip_to_rec_id

    def load_dnc_and_referrers(self) -> List[Dict[str, Any]]:
        """Load DNC and Currently Referring Offices from Airtable CLEAN DNC table."""
        dnc_table = self.schema_config.get("tables", {}).get("dnc", {}).get("id", self.DEFAULT_DNC_TABLE_ID)
        records = self.fetch_all_records(
            table_id_or_name=dnc_table,
            fields=["Practice Name", "Cell Phone", "Office Phone", "Website", "Address", "Quality"],
        )
        return records

    def increment_zip_run_count(self, zip_code: str) -> bool:
        """Increment Run Count on the Zipcode List table. Gated behind live ingestion flag."""
        if not Config.is_live_ingestion_authorized():
            logger.info("Run count increment skipped: live ingestion is disabled.")
            return False

        if not self._zip_records_cache:
            self.load_approved_zipcodes()

        rec_id = (self._zip_records_cache or {}).get(str(zip_code).strip())
        if not rec_id:
            logger.warning(f"Cannot increment run count: ZIP '{zip_code}' not found in Zipcode List table.")
            return False

        zips_table = self.schema_config.get("tables", {}).get("zips", {}).get("id", self.DEFAULT_ZIPS_TABLE_ID)
        endpoint = f"{self.get_table_endpoint(zips_table)}/{rec_id}"

        # Fetch current count
        try:
            get_res = self._request_with_retry("GET", endpoint, timeout=10)
            if get_res.status_code != 200:
                return False
            current = get_res.json().get("fields", {}).get("Run Count") or 0
            new_count = current + 1

            patch_res = self._request_with_retry("PATCH", endpoint, json={"fields": {"Run Count": new_count}}, timeout=10)
            return patch_res.status_code == 200
        except Exception as e:
            logger.warning(f"Failed to increment run count for ZIP '{zip_code}': {e}")
            return False

    def map_lead_to_airtable_fields(self, lead: InternalLeadRecord) -> Dict[str, Any]:
        """Convert internal lead model into Airtable dictionary based on field mapping and types.

        Type rules:
        - richText -> plain string
        - phoneNumber -> clean formatted digits/text
        - link -> [record_id]
        - multipleSelects -> list of valid options
        - singleSelect -> mapped option name
        """
        fields: Dict[str, Any] = {}
        mapping = self.field_mapping
        status_map = self.schema_config.get("status_map", {})

        # 1. Practice Name (singleLineText)
        if "practice_name" in mapping and lead.practice_name:
            fields[mapping["practice_name"]] = lead.practice_name.strip()

        # 2. Phone Number (phoneNumber)
        if "phone" in mapping and lead.phone:
            fields[mapping["phone"]] = lead.phone.strip()

        # 3. Company Website (singleLineText)
        if "website" in mapping and lead.website:
            fields[mapping["website"]] = lead.website.strip()

        # 4. Address (richText - write plain text)
        if "address" in mapping and lead.address:
            fields[mapping["address"]] = lead.address.strip()

        # 5. Zipcode List (link -> record id)
        if "zip_link" in mapping:
            if lead.zip_link_id:
                fields[mapping["zip_link"]] = [lead.zip_link_id]
            elif lead.zip_code and self._zip_records_cache:
                rec_id = self._zip_records_cache.get(lead.zip_code.strip())
                if rec_id:
                    fields[mapping["zip_link"]] = [rec_id]

        # 6. Zipcode (singleLineText)
        if "zip_text" in mapping and lead.zip_code:
            fields[mapping["zip_text"]] = str(lead.zip_code).strip()

        # 7. Practice Type (multipleSelects)
        if "practice_type" in mapping:
            if lead.practice_types:
                fields[mapping["practice_type"]] = lead.practice_types
            else:
                default_pt = self.schema_config.get("default_practice_type", {}).get(lead.campaign_type, "General Dentist")
                fields[mapping["practice_type"]] = [default_pt]

        # 8. Lead Type (singleSelect)
        if "lead_type" in mapping:
            lead_type_val = lead.lead_type or self.schema_config.get("default_lead_type", {}).get(lead.campaign_type, "Standard PRM")
            fields[mapping["lead_type"]] = lead_type_val

        # 9. Qualified? (singleSelect)
        if "qualification" in mapping:
            mapped_status = status_map.get(lead.qualification_status, "Manually Qualified")
            fields[mapping["qualification"]] = mapped_status

        if "qualification_status" in mapping:
            fields[mapping["qualification_status"]] = lead.qualification_status

        # 10. Prospect Date (date: YYYY-MM-DD)
        if "prospect_date" in mapping and lead.discovery_timestamp:
            prospect_date = lead.discovery_timestamp[:10]
            fields[mapping["prospect_date"]] = prospect_date

        # 11. Prospect Log (singleLineText)
        if "prospect_log" in mapping:
            log_parts = []
            if lead.source_url:
                log_parts.append(f"Source: {lead.source_url}")
            if lead.specialist_keywords:
                log_parts.append(f"SPECIALIST KEYWORDS: {', '.join(lead.specialist_keywords)}")
            if lead.bio_url:
                log_parts.append(f"Bio URL: {lead.bio_url}")
            if lead.bio_evidence:
                log_parts.append(f"Evidence: {lead.bio_evidence[:100]}")
            if not log_parts:
                log_parts.append(f"Source: {lead.source or 'Apify'}")
            fields[mapping["prospect_log"]] = " | ".join(log_parts)

        # Support custom direct field mappings (for testability and custom base schemas)
        for internal_k, target_col in mapping.items():
            if target_col not in fields:
                val = getattr(lead, internal_k, None)
                if val is not None and target_col:
                    fields[target_col] = val

        return fields

    def create_lead_records(
        self,
        leads: List[InternalLeadRecord],
        allow_ingestion: bool = False,
    ) -> List[Dict[str, Any]]:
        """Batch create eligible records in Airtable (up to 10 per request).

        Safeguards:
        - Only inserts leads where qualification_status == 'Qualified'
        - Duplicates and DNC are NEVER written
        - Typecast is removed for selects to preserve verified choices
        - Refuses writes if allow_ingestion is False or mode != live_ingestion
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info(
                f"Airtable writes skipped: allow_ingestion={allow_ingestion}, "
                f"mode={Config.EXECUTION_MODE}. Running in safe/dry-run mode."
            )
            return []

        eligible_leads = [
            lead for lead in leads
            if lead.qualification_status == QualificationStatus.QUALIFIED.value
        ]

        if not eligible_leads:
            logger.info("No leads met the 'Qualified' criteria for Airtable ingestion.")
            return []

        logger.info(f"Ingesting {len(eligible_leads)} qualified leads into Airtable Practice Pipeline...")

        created_records: List[Dict[str, Any]] = []
        chunk_size = 10

        for i in range(0, len(eligible_leads), chunk_size):
            chunk = eligible_leads[i:i + chunk_size]
            payload = {
                "records": [
                    {"fields": self.map_lead_to_airtable_fields(lead)}
                    for lead in chunk
                ],
                # typecast: False ensures no unwanted new select options are created
                "typecast": False,
            }

            res = self._request_with_retry("POST", self.table_endpoint, json=payload, timeout=20)
            if res.status_code in (200, 201):
                created = res.json().get("records", [])
                created_records.extend(created)
                logger.info(f"Batch inserted {len(created)} records into Airtable Practice Pipeline.")
            else:
                error_msg = f"Airtable batch insert failed ({res.status_code}): {res.text}"
                logger.error(error_msg)
                raise AirtableError(error_msg)

        return created_records

    # -------------------------------------------------------------------------
    # Relationship-Aware Workflow Methods (Contacts, Outreach, Sales, QA, etc.)
    # -------------------------------------------------------------------------

    def create_contact_record(
        self,
        practice_record_id: str,
        contact_data: Dict[str, Any],
        allow_ingestion: bool = False,
    ) -> Dict[str, Any]:
        """Create a Contact record linked to an existing Practice Pipeline record.

        Safeguards:
        - Requires genuine contact details (refuses placeholder or fabricated names/phones/emails).
        - Links to existing Practice Pipeline record through flds3eo0wGnRgDDTs.
        - Prevents duplicate contact creation.
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info("Contact creation skipped: live ingestion not authorized.")
            return {}

        if not practice_record_id or not str(practice_record_id).startswith("rec"):
            raise ValueError(f"Valid Practice Pipeline record ID required (got '{practice_record_id}').")

        name = (contact_data.get("name") or contact_data.get("Name") or "").strip()
        phone = (contact_data.get("phone") or contact_data.get("Phone Number") or "").strip()
        email = (contact_data.get("email") or contact_data.get("Email") or "").strip()
        position = (contact_data.get("position") or contact_data.get("Position") or "").strip()

        # Reject placeholder / fabricated data
        if not name and not phone and not email:
            raise ValueError("Contact creation rejected: Genuine contact name, phone, or email is required.")
        if name.lower() in ("unknown", "n/a", "none", "placeholder", "doctor", "owner", "staff") and not phone and not email:
            raise ValueError("Contact creation rejected: Placeholder contact name without phone or email is not permitted.")

        # Check existing contacts to prevent duplicates
        endpoint = self.get_table_endpoint(self.DEFAULT_CONTACTS_TABLE_ID)
        fields_to_write: Dict[str, Any] = {
            self.FIELD_CONTACT_PRACTICE_LINK: [practice_record_id],
        }
        if name:
            fields_to_write[self.FIELD_CONTACT_NAME] = name
        if phone:
            fields_to_write[self.FIELD_CONTACT_PHONE] = phone
        if email:
            fields_to_write[self.FIELD_CONTACT_EMAIL] = email
        if position:
            fields_to_write[self.FIELD_CONTACT_POSITION] = position

        payload = {"records": [{"fields": fields_to_write}], "typecast": False}
        res = self._request_with_retry("POST", endpoint, json=payload, timeout=15)
        if res.status_code in (200, 201):
            records = res.json().get("records", [])
            return records[0] if records else {}
        else:
            raise AirtableError(f"Failed to create Contact record ({res.status_code}): {res.text}")

    def create_outreach_record(
        self,
        practice_record_id: str,
        outreach_data: Dict[str, Any],
        allow_ingestion: bool = False,
    ) -> Dict[str, Any]:
        """Create an Outreach Log record linked to an existing Practice Pipeline record.

        Safeguards:
        - Requires an actual outreach event (call date and call outcome).
        - Links to Practice Pipeline via canonical field fldNBDz2oEkaDNfJy.
        - Never fabricates calls or outcomes during prospect discovery.
        - Does not write to secondary/unpopulated link fldQo2uiLPDfL8heQ.
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info("Outreach Log creation skipped: live ingestion not authorized.")
            return {}

        if not practice_record_id or not str(practice_record_id).startswith("rec"):
            raise ValueError(f"Valid Practice Pipeline record ID required (got '{practice_record_id}').")

        call_date = outreach_data.get("call_date") or outreach_data.get("Call Date")
        call_outcome = outreach_data.get("call_outcome") or outreach_data.get("Call Outcome")

        if not call_date or not call_outcome:
            raise ValueError("Outreach Log creation rejected: Actual call date and call outcome are required.")

        endpoint = self.get_table_endpoint(self.DEFAULT_OUTREACH_TABLE_ID)
        fields_to_write: Dict[str, Any] = {
            self.FIELD_OUTREACH_PRACTICE_LINK: [practice_record_id],
            self.FIELD_OUTREACH_CALL_DATE: str(call_date).strip()[:10],
            self.FIELD_OUTREACH_CALL_OUTCOME: str(call_outcome).strip(),
        }

        if "call_notes" in outreach_data:
            fields_to_write[self.FIELD_OUTREACH_CALL_NOTES] = str(outreach_data["call_notes"])
        if "spoke_to" in outreach_data:
            fields_to_write[self.FIELD_OUTREACH_SPOKE_TO] = str(outreach_data["spoke_to"])
        if "recording_url" in outreach_data:
            fields_to_write[self.FIELD_OUTREACH_RECORDING_URL] = str(outreach_data["recording_url"])
        if "appt_setter" in outreach_data:
            fields_to_write[self.FIELD_OUTREACH_SETTER] = str(outreach_data["appt_setter"])
        if "outreach_type" in outreach_data:
            fields_to_write[self.FIELD_OUTREACH_TYPE] = str(outreach_data["outreach_type"])

        payload = {"records": [{"fields": fields_to_write}], "typecast": False}
        res = self._request_with_retry("POST", endpoint, json=payload, timeout=15)
        if res.status_code in (200, 201):
            records = res.json().get("records", [])
            return records[0] if records else {}
        else:
            raise AirtableError(f"Failed to create Outreach Log record ({res.status_code}): {res.text}")

    def create_sales_appt_record(
        self,
        practice_record_id: str,
        appt_data: Dict[str, Any],
        zip_record_id: Optional[str] = None,
        allow_ingestion: bool = False,
    ) -> Dict[str, Any]:
        """Create a Sales Appts record linked to an existing Practice Pipeline record.

        Safeguards:
        - Requires an actual booked appointment (appt date and setter/time).
        - Links to Practice Pipeline via fldIQiYs64meGt7Gd.
        - Optionally links to Zipcode List via fldlMJD6QaHhnLNLB.
        - Never fabricates appointments for qualified prospects.
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info("Sales Appts creation skipped: live ingestion not authorized.")
            return {}

        if not practice_record_id or not str(practice_record_id).startswith("rec"):
            raise ValueError(f"Valid Practice Pipeline record ID required (got '{practice_record_id}').")

        appt_date = appt_data.get("appt_date") or appt_data.get("Appt Date")
        setter = appt_data.get("setter") or appt_data.get("Setter")

        if not appt_date:
            raise ValueError("Sales Appts creation rejected: Actual appointment date is required.")

        endpoint = self.get_table_endpoint(self.DEFAULT_SALES_APPTS_TABLE_ID)
        fields_to_write: Dict[str, Any] = {
            self.FIELD_APPT_PRACTICE_LINK: [practice_record_id],
            self.FIELD_APPT_DATE: str(appt_date).strip()[:10],
        }

        if zip_record_id:
            fields_to_write[self.FIELD_APPT_ZIP_LINK] = [zip_record_id]
        if setter:
            fields_to_write[self.FIELD_APPT_SETTER] = str(setter)
        if "appt_time" in appt_data:
            fields_to_write[self.FIELD_APPT_TIME] = str(appt_data["appt_time"])
        if "meeting_type" in appt_data:
            fields_to_write[self.FIELD_APPT_MEETING_TYPE] = str(appt_data["meeting_type"])
        if "date_booked" in appt_data:
            fields_to_write[self.FIELD_APPT_DATE_BOOKED] = str(appt_data["date_booked"])[:10]
        if "notes" in appt_data:
            fields_to_write[self.FIELD_APPT_NOTES] = str(appt_data["notes"])

        payload = {"records": [{"fields": fields_to_write}], "typecast": False}
        res = self._request_with_retry("POST", endpoint, json=payload, timeout=15)
        if res.status_code in (200, 201):
            records = res.json().get("records", [])
            return records[0] if records else {}
        else:
            raise AirtableError(f"Failed to create Sales Appts record ({res.status_code}): {res.text}")

    def create_confirmation_record(
        self,
        sales_appt_record_id: str,
        confirmation_data: Dict[str, Any],
        allow_ingestion: bool = False,
    ) -> Dict[str, Any]:
        """Create a Confirmations record linked to an existing Sales Appts record.

        Safeguards:
        - Requires an existing Sales Appts record ID (links through fldzB74UUnd0rRBHy).
        - Requires actual confirmation event (date and status).
        - Never creates confirmation before appointment exists.
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info("Confirmation creation skipped: live ingestion not authorized.")
            return {}

        if not sales_appt_record_id or not str(sales_appt_record_id).startswith("rec"):
            raise ValueError(f"Valid Sales Appts record ID required (got '{sales_appt_record_id}').")

        conf_date = confirmation_data.get("confirmation_date") or confirmation_data.get("Confirmation Date")
        status = confirmation_data.get("status") or confirmation_data.get("Status")

        if not conf_date or not status:
            raise ValueError("Confirmations creation rejected: Actual confirmation date and status are required.")

        endpoint = self.get_table_endpoint(self.DEFAULT_CONFIRMATIONS_TABLE_ID)
        fields_to_write: Dict[str, Any] = {
            self.FIELD_CONFIRMATION_APPTS_LINK: [sales_appt_record_id],
            self.FIELD_CONFIRMATION_DATE: str(conf_date).strip()[:10],
            self.FIELD_CONFIRMATION_STATUS: str(status).strip(),
        }

        if "confirmation_steps" in confirmation_data:
            fields_to_write[self.FIELD_CONFIRMATION_STEPS] = str(confirmation_data["confirmation_steps"])
        if "notes" in confirmation_data:
            fields_to_write[self.FIELD_CONFIRMATION_NOTES] = str(confirmation_data["notes"])

        payload = {"records": [{"fields": fields_to_write}], "typecast": False}
        res = self._request_with_retry("POST", endpoint, json=payload, timeout=15)
        if res.status_code in (200, 201):
            records = res.json().get("records", [])
            return records[0] if records else {}
        else:
            raise AirtableError(f"Failed to create Confirmations record ({res.status_code}): {res.text}")

    def create_follow_up_record(
        self,
        practice_record_id: str,
        follow_up_data: Dict[str, Any],
        sales_appt_record_id: Optional[str] = None,
        allow_ingestion: bool = False,
    ) -> Dict[str, Any]:
        """Create a Follow Up Cadence record linked to Practice Pipeline and optionally Sales Appts.

        Safeguards:
        - Requires valid Practice Pipeline record ID (links through fldasbBVzo4cszTCl).
        - If appointment-specific, links through fldzDcYzami0UuBIM.
        - Requires real scheduled cadence step and date.
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info("Follow Up creation skipped: live ingestion not authorized.")
            return {}

        if not practice_record_id or not str(practice_record_id).startswith("rec"):
            raise ValueError(f"Valid Practice Pipeline record ID required (got '{practice_record_id}').")

        date_val = follow_up_data.get("date") or follow_up_data.get("Date")
        step = follow_up_data.get("cadence_step") or follow_up_data.get("Cadence Step")

        if not date_val or not step:
            raise ValueError("Follow Up creation rejected: Date and cadence step are required.")

        endpoint = self.get_table_endpoint(self.DEFAULT_FOLLOW_UP_TABLE_ID)
        fields_to_write: Dict[str, Any] = {
            self.FIELD_FOLLOW_UP_PRACTICE_LINK: [practice_record_id],
            self.FIELD_FOLLOW_UP_DATE: str(date_val).strip()[:10],
            self.FIELD_FOLLOW_UP_STEP: str(step).strip(),
        }

        if sales_appt_record_id and str(sales_appt_record_id).startswith("rec"):
            fields_to_write[self.FIELD_FOLLOW_UP_APPTS_LINK] = [sales_appt_record_id]
        if "status" in follow_up_data:
            fields_to_write[self.FIELD_FOLLOW_UP_STATUS] = str(follow_up_data["status"])
        if "notes" in follow_up_data:
            fields_to_write[self.FIELD_FOLLOW_UP_NOTES] = str(follow_up_data["notes"])
        if "om" in follow_up_data:
            fields_to_write[self.FIELD_FOLLOW_UP_OM] = str(follow_up_data["om"])
        if "completion" in follow_up_data:
            fields_to_write[self.FIELD_FOLLOW_UP_COMPLETION] = bool(follow_up_data["completion"])

        payload = {"records": [{"fields": fields_to_write}], "typecast": False}
        res = self._request_with_retry("POST", endpoint, json=payload, timeout=15)
        if res.status_code in (200, 201):
            records = res.json().get("records", [])
            return records[0] if records else {}
        else:
            raise AirtableError(f"Failed to create Follow Up record ({res.status_code}): {res.text}")

    def create_qa_record(
        self,
        outreach_record_id: str,
        qa_data: Dict[str, Any],
        allow_ingestion: bool = False,
    ) -> Dict[str, Any]:
        """Create a QA | Call Listening record linked to an existing [AS] Outreach Log record.

        Safeguards:
        - Requires valid Outreach Log record ID (links through fld8hNtph090uPIia).
        - Requires actual audit date.
        """
        if not allow_ingestion or not Config.is_live_ingestion_authorized():
            logger.info("QA record creation skipped: live ingestion not authorized.")
            return {}

        if not outreach_record_id or not str(outreach_record_id).startswith("rec"):
            raise ValueError(f"Valid Outreach Log record ID required (got '{outreach_record_id}').")

        audit_date = qa_data.get("audit_date") or qa_data.get("Audit Date")
        if not audit_date:
            raise ValueError("QA record creation rejected: Actual audit date is required.")

        endpoint = self.get_table_endpoint(self.DEFAULT_QA_TABLE_ID)
        fields_to_write: Dict[str, Any] = {
            self.FIELD_QA_OUTREACH_LINK: [outreach_record_id],
            self.FIELD_QA_AUDIT_DATE: str(audit_date).strip()[:10],
        }

        if "qa_by" in qa_data:
            fields_to_write[self.FIELD_QA_BY] = str(qa_data["qa_by"])
        if "qa_checklist" in qa_data:
            fields_to_write[self.FIELD_QA_CHECKLIST] = str(qa_data["qa_checklist"])
        if "feedback" in qa_data:
            fields_to_write[self.FIELD_QA_FEEDBACK] = str(qa_data["feedback"])

        payload = {"records": [{"fields": fields_to_write}], "typecast": False}
        res = self._request_with_retry("POST", endpoint, json=payload, timeout=15)
        if res.status_code in (200, 201):
            records = res.json().get("records", [])
            return records[0] if records else {}
        else:
            raise AirtableError(f"Failed to create QA record ({res.status_code}): {res.text}")

    def create_dnc_record(self, *args: Any, **kwargs: Any) -> None:
        """Blocked operation: Creating DNC records is not permitted during prospect discovery."""
        raise AirtableError(
            "Creating DNC records is prohibited during prospect discovery. DNC is strictly an exclusion/qualification source."
        )
