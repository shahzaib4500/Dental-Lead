"""Deduplication module for dental leads.

Deduplicates leads across:
1. Current batch
2. Existing Airtable / database records

Client Decision Order:
1. Core domain (strip scheme, www., path, query). Shared platforms (facebook, yelp, etc.) ignored.
2. Normalized phone (last 10 digits).
3. Normalized address (street number + street name + suite/unit, USPS abbreviations, no punctuation, plus zip).
4. Place ID as strong secondary key.

Decision Matrix:
- same core domain AND same address -> duplicate, skip.
- same phone AND same address -> duplicate, skip.
- same address, different name/domain/phone -> duplicate, skip.
- same core domain, DIFFERENT address -> controlled by config flag `domain_match_different_address`
  ('new_location' | 'duplicate' | 'needs_review').
- same phone, different name AND different address -> Needs Review (shared building/phone).
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

from config import DuplicateStatus, QualificationStatus
from src.lead_normalizer import InternalLeadRecord

logger = logging.getLogger(__name__)

SHARED_PLATFORM_DOMAINS = {
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "linktr.ee",
    "sites.google.com",
    "yelp.com",
    "yellowpages.com",
    "mapquest.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
}

USPS_STREET_ABBRS = {
    "street": "st",
    "avenue": "ave",
    "boulevard": "blvd",
    "road": "rd",
    "drive": "dr",
    "lane": "ln",
    "court": "ct",
    "place": "pl",
    "terrace": "ter",
    "parkway": "pkwy",
    "highway": "hwy",
    "suite": "ste",
    "apartment": "apt",
    "unit": "unit",
    "building": "bldg",
    "floor": "fl",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northwest": "nw",
    "northeast": "ne",
    "southwest": "sw",
    "southeast": "se",
}


class KnownRecord:
    def __init__(
        self,
        record_id: Optional[str] = None,
        practice_name: str = "",
        core_domain: Optional[str] = None,
        phone_10: Optional[str] = None,
        normalized_address: Optional[str] = None,
        place_id: Optional[str] = None,
    ):
        self.record_id = record_id
        self.practice_name = practice_name
        self.core_domain = core_domain
        self.phone_10 = phone_10
        self.normalized_address = normalized_address
        self.place_id = place_id


class Deduplicator:
    """Manages duplicate detection across batches and existing records."""

    def __init__(
        self,
        existing_records: Optional[List[Dict[str, Any]]] = None,
        domain_match_different_address: str = "new_location",
        shared_platforms: Optional[Set[str]] = None,
    ):
        self.domain_match_different_address = domain_match_different_address
        self.shared_platforms = set(shared_platforms) if shared_platforms else SHARED_PLATFORM_DOMAINS

        # Known record registries
        self.by_place_id: Dict[str, KnownRecord] = {}
        self.by_address: Dict[str, List[KnownRecord]] = {}
        self.by_phone: Dict[str, List[KnownRecord]] = {}
        self.by_domain: Dict[str, List[KnownRecord]] = {}
        self.by_name: Dict[str, List[KnownRecord]] = {}
        self.by_name_address: Set[str] = set()

        if existing_records:
            self.load_existing_records(existing_records)

    @classmethod
    def extract_core_domain(cls, url: Optional[str], shared_platforms: Optional[Set[str]] = None) -> Optional[str]:
        """Strip scheme, www., path, query, and return core registered host."""
        if not url:
            return None
        url_str = str(url).strip()
        if not url_str or url_str.lower() in ("none", "n/a", "null"):
            return None
        try:
            if not url_str.startswith("http://") and not url_str.startswith("https://"):
                url_str = "https://" + url_str
            parsed = urllib.parse.urlparse(url_str)
            host = (parsed.netloc or parsed.path).lower().strip()
            # Remove port if present
            if ":" in host:
                host = host.split(":")[0]
            if host.startswith("www."):
                host = host[4:]
            host = host.rstrip("/")
            if not host:
                return None
            platforms = shared_platforms or SHARED_PLATFORM_DOMAINS
            if host in platforms or any(host.endswith("." + p) for p in platforms):
                return None
            return host
        except Exception:
            return None

    @staticmethod
    def normalize_phone_10(phone: Optional[str]) -> Optional[str]:
        """Normalize telephone number to last 10 digits."""
        if not phone:
            return None
        digits = re.sub(r"\D", "", str(phone))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) >= 10:
            return digits[-10:]
        return None

    @classmethod
    def normalize_address(cls, address: Optional[str], zip_code: Optional[str] = None) -> Optional[str]:
        """Standardize street address components using USPS abbreviations and no punctuation."""
        if not address:
            return None
        raw = str(address).strip()
        # Compare at STREET level so Apify full addresses ("123 Ventura Blvd, Sherman Oaks, CA 91423")
        # match Airtable street-only addresses ("123 Ventura Blvd" + Zipcode column).
        if "," in raw:
            raw = raw.split(",")[0]
        else:
            raw = re.sub(r"[\s,]+[A-Za-z]{2}\s+\d{5}(?:-\d{4})?\s*$", "", raw)
        addr = raw.lower()
        # Remove punctuation
        addr = re.sub(r"[^\w\s]", " ", addr)
        tokens = addr.split()
        normalized_tokens = [USPS_STREET_ABBRS.get(t, t) for t in tokens]
        norm = " ".join(normalized_tokens).strip()
        if not norm:
            return None

        zip_digits = re.sub(r"\D", "", str(zip_code))[:5] if zip_code else ""
        if not zip_digits:
            m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", str(address).strip())
            zip_digits = m.group(1) if m else ""
        if zip_digits and not norm.endswith(zip_digits):
            norm = f"{norm} {zip_digits}"

        return norm or None

    @staticmethod
    def normalize_name(name: Optional[str]) -> str:
        """Strip punctuation, common legal and professional suffixes."""
        if not name:
            return ""
        s = str(name).lower()
        s = re.sub(r"\b(inc|llc|p\.c\.|pc|d\.d\.s\.|dds|d\.m\.d\.|dmd|group|associates|ltd|pllc|pa)\b", "", s)
        s = re.sub(r"[^\w\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def _register_record(self, kr: KnownRecord) -> None:
        """Add record into in-memory indices."""
        if kr.place_id:
            self.by_place_id[kr.place_id] = kr
        if kr.normalized_address:
            self.by_address.setdefault(kr.normalized_address, []).append(kr)
        if kr.phone_10:
            self.by_phone.setdefault(kr.phone_10, []).append(kr)
        if kr.core_domain:
            self.by_domain.setdefault(kr.core_domain, []).append(kr)
        if kr.practice_name:
            norm_name = self.normalize_name(kr.practice_name)
            if norm_name:
                self.by_name.setdefault(norm_name, []).append(kr)
                if kr.normalized_address:
                    self.by_name_address.add(f"{norm_name}___{kr.normalized_address}")

    def load_existing_records(self, records: List[Dict[str, Any]]) -> None:
        """Register already known records from Airtable or local DB."""
        for rec in records:
            # Handle both raw API dictionary with 'fields' and flattened dict
            fields = rec.get("fields", rec)
            rec_id = rec.get("id") or fields.get("record_id")

            # Try real Airtable column names and fallbacks
            name = (
                fields.get("Practice Name")
                or fields.get("practice_name")
                or ""
            )
            phone = (
                fields.get("Phone Number")
                or fields.get("phone")
                or fields.get("Main Phone")
            )
            website = (
                fields.get("Company Website")
                or fields.get("website")
                or fields.get("Website")
            )
            address = (
                fields.get("Address")
                or fields.get("address")
                or fields.get("Street Address")
            )
            zip_code = (
                fields.get("Zipcode")
                or fields.get("zip_code")
                or fields.get("ZIP Code")
            )
            place_id = (
                fields.get("place_id")
                or fields.get("Place ID")
            )

            kr = KnownRecord(
                record_id=rec_id,
                practice_name=str(name).strip(),
                core_domain=self.extract_core_domain(website, self.shared_platforms),
                phone_10=self.normalize_phone_10(phone),
                normalized_address=self.normalize_address(address, zip_code),
                place_id=str(place_id).strip() if place_id else None,
            )
            self._register_record(kr)

    def evaluate_lead(self, lead: InternalLeadRecord) -> InternalLeadRecord:
        """Evaluate lead against known records following the strict decision hierarchy."""
        lead_domain = self.extract_core_domain(lead.website, self.shared_platforms)
        lead_phone_10 = self.normalize_phone_10(lead.phone)
        lead_address = self.normalize_address(lead.address, lead.zip_code)
        lead_place_id = str(lead.place_id).strip() if lead.place_id else None
        lead_name_norm = self.normalize_name(lead.practice_name)

        # 1. Google Place ID match check (strongest unique identifier)
        if lead_place_id and lead_place_id in self.by_place_id:
            matched = self.by_place_id[lead_place_id]
            lead.duplicate_status = DuplicateStatus.CONFIRMED_DUPLICATE.value
            lead.qualification_status = QualificationStatus.DUPLICATE.value
            lead.qualification_reason = f"Duplicate Place ID: {lead_place_id} (matched record {matched.record_id or matched.practice_name})"
            lead.qualification_reasons.append(lead.qualification_reason)
            return lead

        # 2. Normalized Practice Name + Physical Address match
        if lead_name_norm and lead_address:
            name_addr_key = f"{lead_name_norm}___{lead_address}"
            if name_addr_key in self.by_name_address:
                lead.duplicate_status = DuplicateStatus.CONFIRMED_DUPLICATE.value
                lead.qualification_status = QualificationStatus.DUPLICATE.value
                lead.qualification_reason = "Duplicate: Exact practice name and physical address already exists"
                lead.qualification_reasons.append(lead.qualification_reason)
                return lead

        # 3. Physical Address match (same suite/street already exists in pipeline)
        if lead_address and lead_address in self.by_address:
            matched_addr_list = self.by_address[lead_address]
            if matched_addr_list:
                first_match = matched_addr_list[0]
                lead.duplicate_status = DuplicateStatus.CONFIRMED_DUPLICATE.value
                lead.qualification_status = QualificationStatus.DUPLICATE.value
                lead.qualification_reason = f"Duplicate: Physical address '{lead.address}' already exists in pipeline (matched {first_match.record_id or first_match.practice_name})"
                lead.qualification_reasons.append(lead.qualification_reason)
                return lead

        # 4. Supporting evidence: Same core domain AND same address -> duplicate
        if lead_domain and lead_address and lead_domain in self.by_domain:
            for match in self.by_domain[lead_domain]:
                if match.normalized_address == lead_address:
                    lead.duplicate_status = DuplicateStatus.CONFIRMED_DUPLICATE.value
                    lead.qualification_status = QualificationStatus.DUPLICATE.value
                    lead.qualification_reason = f"Duplicate: Same core domain '{lead_domain}' and address '{lead.address}' (matched {match.record_id or match.practice_name})"
                    lead.qualification_reasons.append(lead.qualification_reason)
                    return lead

        # 5. Supporting evidence: Same phone AND same address -> duplicate
        if lead_phone_10 and lead_address and lead_phone_10 in self.by_phone:
            for match in self.by_phone[lead_phone_10]:
                if match.normalized_address == lead_address:
                    lead.duplicate_status = DuplicateStatus.CONFIRMED_DUPLICATE.value
                    lead.qualification_status = QualificationStatus.DUPLICATE.value
                    lead.qualification_reason = f"Duplicate: Same phone '{lead.phone}' and address '{lead.address}' (matched {match.record_id or match.practice_name})"
                    lead.qualification_reasons.append(lead.qualification_reason)
                    return lead

        # 6. Same core domain, DIFFERENT address -> Do not merge separate branches
        if lead_domain and lead_domain in self.by_domain:
            matched_records = self.by_domain[lead_domain]
            first_match = matched_records[0]
            action = self.domain_match_different_address.lower().strip()

            if action == "duplicate":
                lead.duplicate_status = DuplicateStatus.CONFIRMED_DUPLICATE.value
                lead.qualification_status = QualificationStatus.DUPLICATE.value
                lead.qualification_reason = f"Duplicate: Same domain '{lead_domain}' treated as single entity (matched {first_match.record_id or first_match.practice_name})"
                lead.qualification_reasons.append(lead.qualification_reason)
                return lead
            elif action == "new_location":
                lead.duplicate_status = DuplicateStatus.DIFFERENT_LOCATION.value
                if "Additional branch/location" not in lead.qualification_reason:
                    lead.qualification_reason += f" (Additional branch/location of domain '{lead_domain}': {first_match.record_id or first_match.practice_name})"
                lead.qualification_reasons.append(f"Additional branch/location of known domain '{lead_domain}'")
                self._register_lead(lead)
                return lead
            else:  # "needs_review"
                lead.duplicate_status = DuplicateStatus.POTENTIAL_DUPLICATE.value
                lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
                lead.qualification_reason = f"Needs Review: Same domain '{lead_domain}' at different address '{lead.address}' (matched {first_match.record_id or first_match.practice_name})"
                lead.qualification_reasons.append(lead.qualification_reason)
                self._register_lead(lead)
                return lead

        # 7. Same phone, different name AND different address -> Needs Review (shared building/phone)
        if lead_phone_10 and lead_phone_10 in self.by_phone:
            matched_phone_list = self.by_phone[lead_phone_10]
            first_match = matched_phone_list[0]
            if first_match.normalized_address != lead_address and self.normalize_name(first_match.practice_name) != lead_name_norm:
                lead.duplicate_status = DuplicateStatus.POTENTIAL_DUPLICATE.value
                lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
                lead.qualification_reason = f"Needs Review: Shared phone with '{first_match.practice_name}' at different address (matched {first_match.record_id or first_match.practice_name})"
                lead.qualification_reasons.append(lead.qualification_reason)
                self._register_lead(lead)
                return lead

        # 8. Ambiguous practice match: same name, but address different or missing
        if lead_name_norm and lead_name_norm in self.by_name:
            matched_names = self.by_name[lead_name_norm]
            first_match = matched_names[0]
            lead.duplicate_status = DuplicateStatus.POTENTIAL_DUPLICATE.value
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = f"Needs Review: Ambiguous practice match with '{first_match.practice_name}' (matched {first_match.record_id or first_match.practice_name})"
            lead.qualification_reasons.append(lead.qualification_reason)
            self._register_lead(lead)
            return lead

        # Unique Lead
        lead.duplicate_status = DuplicateStatus.UNIQUE.value
        self._register_lead(lead)
        return lead

    def _register_lead(self, lead: InternalLeadRecord) -> None:
        """Register newly evaluated lead into in-memory indices."""
        kr = KnownRecord(
            record_id=lead.place_id or None,
            practice_name=lead.practice_name,
            core_domain=self.extract_core_domain(lead.website, self.shared_platforms),
            phone_10=self.normalize_phone_10(lead.phone),
            normalized_address=self.normalize_address(lead.address, lead.zip_code),
            place_id=str(lead.place_id).strip() if lead.place_id else None,
        )
        self._register_record(kr)

    def deduplicate_batch(self, leads: List[InternalLeadRecord]) -> List[InternalLeadRecord]:
        """Process an entire batch of leads, checking against existing records and each other."""
        processed: List[InternalLeadRecord] = []
        for lead in leads:
            processed.append(self.evaluate_lead(lead))
        return processed