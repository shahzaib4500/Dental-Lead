"""Lead qualification module.

Applies campaign qualification rules using settings loaded from JSON:
- config/qualification_rules.json
- config/dso_keywords.json
- config/excluded_chains.json
- config/territories.json
- config/existing_referrers.json
- config/do_not_contact.json

Preserves core PRM and Wisdom Sales campaign definitions.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import List, Optional, Tuple

from config import CampaignType, QualificationStatus
from src.config_loader import AppConfigBundle
from src.lead_normalizer import InternalLeadRecord

logger = logging.getLogger(__name__)


class LeadQualifier:
    """Applies campaign qualification rules to internal lead records."""

    def __init__(self, config_bundle: AppConfigBundle):
        self.bundle = config_bundle

    def qualify(self, lead: InternalLeadRecord) -> InternalLeadRecord:
        """Run lead qualification pipeline based on loaded JSON configuration."""
        # 1. Existing Referrer or Do-Not-Contact exclusion check (Mandatory baseline check)
        is_dnc, dnc_reason = self._check_dnc_list(lead)
        if is_dnc:
            lead.qualification_status = QualificationStatus.EXISTING_REFERRER_OR_DNC.value
            lead.qualification_reason = dnc_reason
            return lead

        is_ref, ref_reason = self._check_referrer_list(lead)
        if is_ref:
            lead.qualification_status = QualificationStatus.EXISTING_REFERRER_OR_DNC.value
            lead.qualification_reason = ref_reason
            return lead

        # 2. Check for required contact information
        has_contact, contact_reason = self._check_required_fields(lead)
        if not has_contact:
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = contact_reason
            return lead

        # Check for Address/ZIP conflict
        for issue in lead.cleaning_issues:
            if "address/zip conflict" in issue.lower():
                lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
                lead.qualification_reason = issue
                return lead

        # 3. Territory check
        territory_ok, territory_reason = self._check_territory(lead)
        if not territory_ok:
            if "unconfigured" in territory_reason.lower() or "missing zip" in territory_reason.lower():
                lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
                lead.qualification_reason = territory_reason
                return lead
            else:
                lead.qualification_status = QualificationStatus.DISQUALIFIED.value
                lead.qualification_reason = territory_reason
                return lead

        # 4. Campaign-specific rules
        campaign_name = lead.campaign_type
        if campaign_name == CampaignType.WISDOM_SALES.value:
            return self._qualify_wisdom_sales(lead)
        else:
            return self._qualify_prm(lead)

    def _check_dnc_list(self, lead: InternalLeadRecord) -> Tuple[bool, str]:
        """Check against active Do-Not-Contact list from JSON."""
        return self._match_entity_entries(lead, self.bundle.do_not_contact.entries, "Do-Not-Contact")

    def _check_referrer_list(self, lead: InternalLeadRecord) -> Tuple[bool, str]:
        """Check against active Existing Referrer list from JSON."""
        return self._match_entity_entries(lead, self.bundle.existing_referrers.entries, "Existing Referrer")

    @staticmethod
    def _match_entity_entries(
        lead: InternalLeadRecord,
        entries: List[Any],
        label: str,
    ) -> Tuple[bool, str]:
        lead_name = lead.practice_name.lower().strip()
        lead_phone_digits = re.sub(r"\D", "", lead.phone or "")
        lead_place_id = (lead.place_id or "").strip()
        lead_domain = ""
        if lead.website:
            try:
                host = urllib.parse.urlparse(lead.website).netloc.lower()
                lead_domain = host[4:] if host.startswith("www.") else host
            except Exception:
                pass

        for entry in entries:
            if not getattr(entry, "active", True):
                continue
            etype = entry.type.lower()
            val = str(entry.value).strip()
            if not val:
                continue

            if etype == "name":
                norm_lead = re.sub(r"[^\w\s]", "", lead_name).strip()
                norm_val = re.sub(r"[^\w\s]", "", val.lower()).strip()
                if norm_lead == norm_val:
                    return True, f"Matched {label} list by name: '{val}'"
                # Check token-set similarity
                lead_tokens = set(norm_lead.split())
                val_tokens = set(norm_val.split())
                # If both have meaningful tokens and high overlap (e.g. jaccard >= 0.75 or exact containment of multi-word phrase)
                if len(val_tokens) >= 2 and val_tokens.issubset(lead_tokens):
                    return True, f"Matched {label} list by name: '{val}'"
                intersection = lead_tokens.intersection(val_tokens)
                union = lead_tokens.union(val_tokens)
                if union and (len(intersection) / len(union)) >= 0.75:
                    return True, f"Matched {label} list by name: '{val}'"
            elif etype == "phone":
                val_digits = re.sub(r"\D", "", val)
                if len(val_digits) == 11 and val_digits.startswith("1"):
                    val_digits = val_digits[1:]
                lead_digits = lead_phone_digits
                if len(lead_digits) == 11 and lead_digits.startswith("1"):
                    lead_digits = lead_digits[1:]
                if val_digits and lead_digits and val_digits[-10:] == lead_digits[-10:]:
                    return True, f"Matched {label} list by phone: '{val}'"
            elif etype == "domain":
                clean_domain = val.lower().replace("https://", "").replace("http://", "").strip("/")
                if clean_domain.startswith("www."):
                    clean_domain = clean_domain[4:]
                if clean_domain and lead_domain and (clean_domain == lead_domain or lead_domain.endswith("." + clean_domain)):
                    return True, f"Matched {label} list by domain: '{val}'"
            elif etype == "place_id" and lead_place_id and val == lead_place_id:
                return True, f"Matched {label} list by Place ID: '{val}'"

        return False, ""

    def _check_required_fields(self, lead: InternalLeadRecord) -> Tuple[bool, str]:
        """Verify presence of minimal required fields based on JSON configuration."""
        req_fields = self.bundle.qualification_rules.required_google_maps_fields
        missing = []
        for field in req_fields:
            val = getattr(lead, field, None)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(field)

        if missing:
            return False, f"Missing required Google Maps fields: {', '.join(missing)}"
        return True, ""

    def _check_territory(self, lead: InternalLeadRecord) -> Tuple[bool, str]:
        """Validate lead against approved ZIP codes, cities, or states from JSON."""
        cfg = self.bundle.territories
        if not cfg.require_approved_territory:
            return True, "Territory restriction disabled"

        has_any_territory = bool(cfg.approved_zip_codes or cfg.approved_cities or cfg.approved_states)
        if not has_any_territory:
            return False, "Territory unconfigured: Approved territories pending in config/territories.json"

        # Check if lead matches ANY of the configured territory criteria
        matched_criteria = []
        if cfg.approved_zip_codes and lead.zip_code and lead.zip_code in cfg.approved_zip_codes:
            matched_criteria.append(f"ZIP ({lead.zip_code})")
        if cfg.approved_cities and lead.city and lead.city.lower() in [c.lower() for c in cfg.approved_cities]:
            matched_criteria.append(f"City ({lead.city})")
        if cfg.approved_states and lead.state and lead.state.upper() in [s.upper() for s in cfg.approved_states]:
            matched_criteria.append(f"State ({lead.state})")

        if matched_criteria:
            return True, f"Within approved territory by {', '.join(matched_criteria)}"

        # If lead has no matching attributes
        return False, "Outside approved service territory"

    def _detect_dso_affiliation(self, lead: InternalLeadRecord) -> Tuple[bool, Optional[str]]:
        """Detect possible DSO keywords from config/dso_keywords.json."""
        text = f"{lead.practice_name} {lead.website or ''}".lower()
        for kw in self.bundle.dso_keywords.keywords:
            clean_kw = kw.strip().lower()
            if not clean_kw:
                continue
            if re.search(r"\b" + re.escape(clean_kw) + r"\b", text):
                return True, kw
        return False, None

    def _qualify_prm(self, lead: InternalLeadRecord) -> InternalLeadRecord:
        """PRM campaign qualification logic (General, Family, Comprehensive)."""
        prm_cfg = self.bundle.qualification_rules.campaigns.get("PRM")
        target_cats = prm_cfg.target_categories if prm_cfg else [
            "general dentist", "family dentist", "comprehensive dentist"
        ]

        # 1. DSO check
        is_dso, dso_name = self._detect_dso_affiliation(lead)
        if is_dso:
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = f"Possible DSO keyword match ({dso_name}) - Review required"
            return lead

        # 2. Check for specialist keywords found on doctor bios
        if lead.specialist_keywords:
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = f"Specialist keywords on Dr bio/website ({', '.join(lead.specialist_keywords[:3])}) - Not clean GP"
            return lead

        # 3. Category match
        text = f"{lead.category or ''} {lead.search_keyword} {lead.practice_name}".lower()
        matched = [cat for cat in target_cats if cat.lower() in text]

        if matched:
            lead.qualification_status = QualificationStatus.QUALIFIED.value
            lead.qualification_reason = f"PRM target category match: {', '.join(matched)}"
        else:
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = "PRM review: Unconfirmed category or non-general practice"

        return lead

    def _qualify_wisdom_sales(self, lead: InternalLeadRecord) -> InternalLeadRecord:
        """Wisdom Sales campaign qualification logic (Dental specialists)."""
        ws_cfg = self.bundle.qualification_rules.campaigns.get("WISDOM_SALES")
        target_cats = ws_cfg.target_categories if ws_cfg else [
            "periodontist", "oral surgeon", "oral and maxillofacial surgeon", "prosthodontist", "jaw surgeon"
        ]

        text_to_check = f"{lead.practice_name} {lead.website or ''}".lower()

        # 1. Exclude national chains (from config/excluded_chains.json)
        for chain in self.bundle.excluded_chains.wisdom_sales:
            if chain.lower().strip() in text_to_check:
                lead.qualification_status = QualificationStatus.DISQUALIFIED.value
                lead.qualification_reason = f"Wisdom Sales exclusion: National chain ({chain})"
                return lead

        # 2. DSO check
        is_dso, dso_name = self._detect_dso_affiliation(lead)
        if is_dso:
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = f"Possible DSO keyword match ({dso_name}) - Review required"
            return lead

        # 3. Target specialist match
        combined_text = f"{lead.category or ''} {lead.search_keyword} {lead.practice_name}".lower()
        matched = [cat for cat in target_cats if cat.lower() in combined_text]

        if matched:
            lead.qualification_status = QualificationStatus.QUALIFIED.value
            lead.qualification_reason = f"Wisdom Sales specialist match: {', '.join(matched)}"
        else:
            lead.qualification_status = QualificationStatus.NEEDS_REVIEW.value
            lead.qualification_reason = "Wisdom Sales review: Specialty unconfirmed in listing"

        return lead
