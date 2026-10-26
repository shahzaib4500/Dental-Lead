"""Normalizer module for Google Maps raw results into consistent internal lead records.

Extracts practice details from Apify Actor (nwua9Gu5YrADL7ZDj - compass/crawler-google-places)
and standard Google Maps formats. Parses address parts, formats telephone numbers,
and cleans websites without fabricating missing data.
"""

from __future__ import annotations

import datetime
import re
import urllib.parse
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class InternalLeadRecord(BaseModel):
    """Consistent internal representation of a discovered dental lead."""
    practice_name: str
    website: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    google_maps_url: Optional[str] = None
    place_id: Optional[str] = None
    category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    search_keyword: str
    campaign_type: str
    qualification_status: str = "Needs Review"
    qualification_reason: str = "Discovered - pending qualification"
    duplicate_status: str = "Unique"
    discovery_timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    source: str = "Google Maps via Apify Actor"

    # Quality, Audit, and Normalization fields
    raw_source_record: Dict[str, Any] = Field(default_factory=dict)
    raw_phone: Optional[str] = None
    normalized_name: str = ""
    normalized_phone: Optional[str] = None
    normalized_domain: Optional[str] = None
    normalized_address: Optional[str] = None
    cleaning_issues: List[str] = Field(default_factory=list)
    qualification_reasons: List[str] = Field(default_factory=list)
    blocker_ids: List[str] = Field(default_factory=list)
    deduplication_key: Optional[str] = None
    source_url: Optional[str] = None
    campaign: Optional[str] = None
    processed_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    # Airtable-specific linked & categorized fields
    zip_link_id: Optional[str] = None
    practice_types: List[str] = Field(default_factory=list)
    lead_type: Optional[str] = None
    specialist_keywords: List[str] = Field(default_factory=list)
    bio_url: Optional[str] = None
    bio_evidence: Optional[str] = None
    qualification_fact_tag: str = "Unknown"  # "Confirmed", "Inferred", or "Unknown"


class LeadNormalizer:
    """Normalizes raw Apify Google Maps listings into structured InternalLeadRecord instances."""

    US_STATE_ABBRS = {
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"
    }

    @staticmethod
    def strip_control_chars(text: Optional[str]) -> str:
        """Strip ASCII control characters (0x00-0x1f, 0x7f-0x9f)."""
        if not text:
            return ""
        # Remove control characters except standard whitespace if needed, or all control chars
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", str(text))
        return cleaned

    @classmethod
    def clean_name(cls, name: Optional[str]) -> str:
        """Strip control characters, collapse whitespace, and normalize practice name."""
        if not name:
            return ""
        cleaned = cls.strip_control_chars(name)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @classmethod
    def normalize_name_for_dedup(cls, name: Optional[str]) -> str:
        """Normalized comparison string for practice names."""
        if not name:
            return ""
        s = cls.clean_name(name).lower()
        s = re.sub(r"\b(inc|llc|p\.c\.|pc|d\.d\.s\.|dds|d\.m\.d\.|dmd|group|associates|ltd|pllc|pa)\b", "", s)
        s = re.sub(r"[^\w\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    @staticmethod
    def clean_phone(phone: Optional[str]) -> Optional[str]:
        """Normalize telephone number to standard digits or (XXX) XXX-XXXX format."""
        if not phone:
            return None
        raw_digits = re.sub(r"\D", "", str(phone))
        # Handle 11 digits starting with 1 (US country code)
        if len(raw_digits) == 11 and raw_digits.startswith("1"):
            raw_digits = raw_digits[1:]
        if len(raw_digits) == 10:
            return f"({raw_digits[0:3]}) {raw_digits[3:6]}-{raw_digits[6:10]}"
        elif len(raw_digits) >= 7:
            return raw_digits
        return None

    @classmethod
    def extract_domain(cls, url: Optional[str]) -> Optional[str]:
        """Extract canonical host domain (without www.) from a URL."""
        if not url:
            return None
        cleaned_url = cls.clean_website(url)
        if not cleaned_url:
            return None
        try:
            parsed = urllib.parse.urlparse(cleaned_url)
            host = parsed.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            return host or None
        except Exception:
            return None

    @classmethod
    def clean_website(cls, url: Optional[str]) -> Optional[str]:
        """Clean and validate website URL. Avoid Google Maps redirect URLs."""
        if not url:
            return None
        url_str = cls.strip_control_chars(url).strip()
        if not url_str or url_str.lower() in ("none", "null", "n/a"):
            return None
        # Handle google maps redirect urls (e.g. google.com/url?q=...)
        if "google.com/url" in url_str:
            parsed = urllib.parse.urlparse(url_str)
            qs = urllib.parse.parse_qs(parsed.query)
            if "q" in qs and qs["q"]:
                url_str = qs["q"][0]

        if not url_str.startswith("http://") and not url_str.startswith("https://"):
            url_str = f"https://{url_str}"

        try:
            parsed = urllib.parse.urlparse(url_str)
            host = parsed.netloc.strip().lower()
            # Valid domain must contain a period, no whitespace, and valid hostname characters
            if host and "." in host and not re.search(r"\s", host):
                # Ensure TLD is at least 2 chars
                parts = host.split(":")  # ignore port if present
                domain_parts = parts[0].split(".")
                if len(domain_parts) >= 2 and len(domain_parts[-1]) >= 2:
                    return url_str
        except Exception:
            pass
        return None

    @classmethod
    def parse_address_string(cls, address_raw: str) -> Dict[str, Optional[str]]:
        """Parse components (street, city, state, zip) from a single address string."""
        res: Dict[str, Optional[str]] = {
            "street": None,
            "city": None,
            "state": None,
            "zip_code": None,
        }
        if not address_raw:
            return res

        address_raw = cls.strip_control_chars(address_raw).strip()

        # Look for standard US State + 5-digit ZIP pattern (e.g. "CA 90210" or "TX 78701")
        zip_match = re.search(r"\b([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b", address_raw)
        if zip_match:
            state_cand = zip_match.group(1).upper()
            if state_cand in cls.US_STATE_ABBRS:
                res["state"] = state_cand
            res["zip_code"] = str(zip_match.group(2)[:5])
        else:
            # Standalone 5-digit zip
            zip_only = re.search(r"\b(\d{5})\b", address_raw)
            if zip_only:
                res["zip_code"] = str(zip_only.group(1))

        # Split comma segments
        parts = [p.strip() for p in address_raw.split(",") if p.strip()]
        if len(parts) >= 3:
            res["street"] = parts[0]
            res["city"] = parts[1]
            if not res["state"] and len(parts) >= 3:
                state_cand = parts[2].split()[0].upper()
                if state_cand in cls.US_STATE_ABBRS:
                    res["state"] = state_cand
        elif len(parts) == 2:
            res["street"] = parts[0]
            res["city"] = parts[1]
        elif len(parts) == 1:
            res["street"] = parts[0]

        return res

    @classmethod
    def normalize_lead(
        cls,
        raw_data: Dict[str, Any],
        search_keyword: str,
        campaign_type: str,
    ) -> InternalLeadRecord:
        """Convert a raw Apify Google Maps item into a standardized InternalLeadRecord.

        Supports Apify Actor nwua9Gu5YrADL7ZDj output schema:
        - title / name -> practice_name
        - placeId / id / cid -> place_id
        - categoryName / categories / type -> category
        - phone / phoneUnformatted -> phone
        - website / url -> website
        - url (google maps link) -> google_maps_url
        - street, city, state, postalCode, address -> address components
        - location: {lat, lng} / latitude / longitude -> coordinates
        """
        cleaning_issues: List[str] = []

        # 1. Practice Name
        raw_name = (
            raw_data.get("title")
            or raw_data.get("name")
            or raw_data.get("business_name")
        )
        name = cls.clean_name(raw_name)
        if not name or name.lower() in ("unknown practice", "none", "n/a"):
            name = "Unknown Practice"
            cleaning_issues.append("Missing or unparseable practice name")

        normalized_name = cls.normalize_name_for_dedup(name)

        # 2. Category
        category_str: Optional[str] = None
        if raw_data.get("categoryName"):
            category_str = str(raw_data["categoryName"])
        elif raw_data.get("categories"):
            cats = raw_data["categories"]
            category_str = ", ".join(str(c) for c in cats) if isinstance(cats, list) else str(cats)
        elif raw_data.get("type"):
            types = raw_data["type"]
            category_str = ", ".join(str(t) for t in types) if isinstance(types, list) else str(types)

        if category_str:
            category_str = cls.clean_name(category_str)

        # 3. Phone Number & Raw Phone Preservation
        raw_phone = raw_data.get("phone") or raw_data.get("phoneUnformatted") or raw_data.get("telephone")
        phone: Optional[str] = None
        normalized_phone: Optional[str] = None
        if raw_phone:
            raw_phone_str = cls.strip_control_chars(str(raw_phone)).strip()
            phone = cls.clean_phone(raw_phone_str)
            digits = re.sub(r"\D", "", raw_phone_str)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 10:
                normalized_phone = digits
            elif len(digits) >= 7:
                normalized_phone = digits
            else:
                cleaning_issues.append(f"Malformed or short phone number: '{raw_phone_str}'")
        else:
            cleaning_issues.append("Missing phone number")

        # 4. Website and Domain Extraction
        raw_website = raw_data.get("website")
        raw_url = raw_data.get("url")
        website = cls.clean_website(raw_website)

        # Fallback if website is not in 'website' key
        if not website and raw_url and "google.com/maps" not in str(raw_url):
            website = cls.clean_website(raw_url)

        normalized_domain = cls.extract_domain(website) if website else None
        if raw_website and not website:
            cleaning_issues.append(f"Invalid or unparseable website URL: '{raw_website}'")
        elif not website:
            cleaning_issues.append("Missing website")

        # 5. Google Maps URL
        maps_url: Optional[str] = None
        if raw_url and "google.com/maps" in str(raw_url):
            maps_url = str(raw_url)
        elif raw_data.get("googleMapsUrl"):
            maps_url = str(raw_data["googleMapsUrl"])
        elif raw_data.get("google_maps_url"):
            maps_url = str(raw_data["google_maps_url"])

        # 6. Place ID / Unique Identifier
        place_id = (
            raw_data.get("placeId")
            or raw_data.get("place_id")
            or raw_data.get("dataId")
            or raw_data.get("cid")
            or raw_data.get("id")
        )
        if place_id:
            place_id = cls.strip_control_chars(str(place_id)).strip()

        # If maps_url is missing but place_id is available, construct canonical URL
        if not maps_url and place_id:
            maps_url = f"https://www.google.com/maps/place/?q=place_id:{place_id}"

        # 7. Coordinates (Latitude / Longitude)
        lat: Optional[float] = None
        lng: Optional[float] = None
        loc_obj = raw_data.get("location")
        if isinstance(loc_obj, dict):
            lat = loc_obj.get("lat") or loc_obj.get("latitude")
            lng = loc_obj.get("lng") or loc_obj.get("longitude")
        if lat is None and raw_data.get("latitude") is not None:
            lat = raw_data.get("latitude")
        if lng is None and raw_data.get("longitude") is not None:
            lng = raw_data.get("longitude")

        try:
            lat = float(lat) if lat is not None else None
            lng = float(lng) if lng is not None else None
        except (ValueError, TypeError):
            lat, lng = None, None

        # 8. Address Processing (Structured fields from Apify or parsed from address string)
        address_line = raw_data.get("address")
        if isinstance(address_line, list):
            address_line = ", ".join(cls.strip_control_chars(str(p)).strip() for p in address_line if p)
        elif address_line:
            address_line = cls.strip_control_chars(str(address_line)).strip()

        parsed = cls.parse_address_string(address_line or "")

        raw_street = raw_data.get("street") or parsed.get("street")
        raw_city = raw_data.get("city") or parsed.get("city")
        raw_state = raw_data.get("state") or parsed.get("state")

        supplied_zip = raw_data.get("postalCode") or raw_data.get("zip")
        parsed_zip = parsed.get("zip_code")
        if supplied_zip and parsed_zip:
            clean_supplied = cls.strip_control_chars(str(supplied_zip)).strip()[:5]
            clean_parsed = cls.strip_control_chars(str(parsed_zip)).strip()[:5]
            if clean_supplied != clean_parsed:
                cleaning_issues.append(
                    f"Address/ZIP conflict: address contains '{clean_parsed}' but supplied ZIP is '{clean_supplied}'"
                )

        raw_zip = supplied_zip or parsed_zip

        street = cls.clean_name(raw_street) if raw_street else None
        city = cls.clean_name(raw_city) if raw_city else None
        state = cls.strip_control_chars(str(raw_state)).strip().upper() if raw_state else None
        zip_code = cls.strip_control_chars(str(raw_zip)).strip()[:5] if raw_zip else None

        full_address = address_line or ", ".join([p for p in [street, city, state, zip_code] if p]) or None
        if not full_address:
            cleaning_issues.append("Missing physical address")

        normalized_address = None
        if full_address:
            # Deterministic comparison address
            s_addr = full_address.lower()
            s_addr = re.sub(r"[^\w\s]", " ", s_addr)
            normalized_address = re.sub(r"\s+", " ", s_addr).strip()

        # Deduplication key: place_id if present, else normalized_name___normalized_address
        dedup_key = place_id if place_id else (f"{normalized_name}___{normalized_address}" if normalized_address else normalized_name)

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return InternalLeadRecord(
            practice_name=name,
            website=website,
            phone=phone,
            address=full_address,
            city=city,
            state=state,
            zip_code=zip_code,
            google_maps_url=maps_url,
            place_id=place_id,
            category=category_str,
            latitude=lat,
            longitude=lng,
            search_keyword=search_keyword,
            campaign_type=campaign_type,
            qualification_status="Needs Review",
            qualification_reason="Discovered - pending qualification",
            duplicate_status="Unique",
            source="Google Maps via Apify Actor",
            raw_source_record=raw_data,
            raw_phone=str(raw_phone) if raw_phone else None,
            normalized_name=normalized_name,
            normalized_phone=normalized_phone,
            normalized_domain=normalized_domain,
            normalized_address=normalized_address,
            cleaning_issues=cleaning_issues,
            qualification_reasons=["Discovered - pending qualification"],
            blocker_ids=[],
            deduplication_key=dedup_key,
            source_url=maps_url,
            campaign=campaign_type,
            processed_at=now_iso,
        )
