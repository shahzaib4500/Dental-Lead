"""Blocker evaluation engine.

Evaluates configurable blockers from config/blockers.json against discovered leads.
- Blockers can be individually enabled or disabled via JSON.
- Handlers evaluate known conditions:
  - no_approved_territory
  - missing_practice_name
  - missing_address
  - missing_phone
  - missing_website
  - dso_keyword_match
  - existing_referrer_match
  - do_not_contact_match
  - duplicate_match
  - chain_exclusion_match

CRITICAL SAFETY RULE:
Disabling a configurable blocker in JSON NEVER bypasses mandatory protections
(DNC, duplicate exclusion, disabled live ingestion, or invalid credentials).
Mandatory safety checks are enforced independently in code.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from config import QualificationStatus
from src.config_loader import AppConfigBundle, BlockerDefinition
from src.lead_normalizer import InternalLeadRecord

logger = logging.getLogger(__name__)


class BlockerEvaluationResult:
    """Holds the result of blocker evaluations for a single lead."""

    def __init__(self, lead: InternalLeadRecord):
        self.lead = lead
        self.triggered_blocker_ids: List[str] = []
        self.blocker_messages: List[str] = []
        self.matched_dso_keyword: Optional[str] = None
        self.duplicate_match_reason: Optional[str] = None
        self.exclusion_reason: Optional[str] = None
        self.ingestion_permitted: bool = False
        self.highest_severity: Optional[str] = None  # "block", "review", or None


class BlockerEngine:
    """Executes registered condition handlers against leads based on active blockers."""

    def __init__(self, config_bundle: AppConfigBundle):
        self.bundle = config_bundle
        self._handlers: Dict[str, Callable[[InternalLeadRecord, BlockerDefinition, BlockerEvaluationResult], bool]] = {
            "no_approved_territory": self._check_territory_condition,
            "missing_practice_name": self._check_missing_practice_name,
            "missing_address": self._check_missing_address,
            "missing_phone": self._check_missing_phone,
            "missing_website": self._check_missing_website,
            "dso_keyword_match": self._check_dso_keyword_match,
            "existing_referrer_match": self._check_existing_referrer_match,
            "do_not_contact_match": self._check_do_not_contact_match,
            "duplicate_match": self._check_duplicate_match,
            "chain_exclusion_match": self._check_chain_exclusion_match,
        }

    def evaluate_lead(self, lead: InternalLeadRecord) -> BlockerEvaluationResult:
        """Evaluate all enabled blockers applicable to the lead's campaign."""
        result = BlockerEvaluationResult(lead)

        for blocker in self.bundle.blockers.blockers:
            if not blocker.enabled:
                continue

            # Check if blocker applies to this campaign
            if blocker.applies_to and lead.campaign_type not in blocker.applies_to:
                continue

            handler = self._handlers.get(blocker.condition)
            if not handler:
                logger.error(f"Handler for condition '{blocker.condition}' is not implemented.")
                continue

            # Execute condition handler
            is_triggered = handler(lead, blocker, result)
            if is_triggered:
                result.triggered_blocker_ids.append(blocker.id)
                result.blocker_messages.append(f"[{blocker.id}] {blocker.message}")

                if blocker.severity == "block":
                    result.highest_severity = "block"
                elif blocker.severity == "review" and result.highest_severity != "block":
                    result.highest_severity = "review"

                # Update lead status and reason
                if blocker.action == "skip":
                    lead.qualification_status = QualificationStatus.DISQUALIFIED.value
                elif blocker.status:
                    lead.qualification_status = blocker.status
                lead.qualification_reason = f"{blocker.message} ({blocker.id})"

        # Determine if ingestion is permitted according to blockers
        # Ingestion is only permitted if no blocking or review blockers were triggered
        # AND lead is strictly Qualified
        if (
            not result.triggered_blocker_ids
            and lead.qualification_status == QualificationStatus.QUALIFIED.value
        ):
            result.ingestion_permitted = True
        else:
            result.ingestion_permitted = False

        return result

    # -------------------------------------------------------------------------
    # Condition Handlers
    # -------------------------------------------------------------------------

    def _check_territory_condition(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        """Triggered if territories are required but unconfigured or lead is out of territory."""
        cfg = self.bundle.territories
        if not cfg.require_approved_territory:
            return False

        has_any_territory = bool(cfg.approved_zip_codes or cfg.approved_cities or cfg.approved_states)
        if not has_any_territory:
            # Unconfigured territory: blocks automatic ingestion
            return True

        # Check if lead matches ANY of the configured territory criteria
        if cfg.approved_zip_codes and lead.zip_code and lead.zip_code in cfg.approved_zip_codes:
            return False
        if cfg.approved_cities and lead.city and lead.city.lower() in [c.lower() for c in cfg.approved_cities]:
            return False
        if cfg.approved_states and lead.state and lead.state.upper() in [s.upper() for s in cfg.approved_states]:
            return False

        return True

    def _check_missing_practice_name(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        return not lead.practice_name or lead.practice_name.lower() in ("unknown practice", "")

    def _check_missing_address(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        return not lead.address or not lead.address.strip()

    def _check_missing_phone(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        return not lead.phone or not lead.phone.strip()

    def _check_missing_website(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        return not lead.website or not lead.website.strip()

    def _check_dso_keyword_match(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        """Check for possible DSO keyword affiliation."""
        text = f"{lead.practice_name} {lead.website or ''}".lower()
        for kw in self.bundle.dso_keywords.keywords:
            clean_kw = kw.strip().lower()
            if not clean_kw:
                continue
            if re.search(r"\b" + re.escape(clean_kw) + r"\b", text):
                result.matched_dso_keyword = kw
                return True
        return False

    def _check_existing_referrer_match(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        """Check against active existing referrers."""
        return self._matches_entity_list(lead, self.bundle.existing_referrers.entries, result)

    def _check_do_not_contact_match(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        """Check against active do-not-contact entries."""
        return self._matches_entity_list(lead, self.bundle.do_not_contact.entries, result)

    def _check_duplicate_match(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        """Triggered if lead is marked as confirmed duplicate."""
        if lead.duplicate_status == "Confirmed Duplicate":
            result.duplicate_match_reason = lead.qualification_reason
            return True
        return False

    def _check_chain_exclusion_match(
        self,
        lead: InternalLeadRecord,
        blocker: BlockerDefinition,
        result: BlockerEvaluationResult,
    ) -> bool:
        """Check for excluded chains (scoped to Wisdom Sales or configured campaigns)."""
        if lead.campaign_type == "WISDOM_SALES":
            text = f"{lead.practice_name} {lead.website or ''}".lower()
            for chain in self.bundle.excluded_chains.wisdom_sales:
                if chain.lower().strip() in text:
                    result.exclusion_reason = f"Excluded chain ({chain})"
                    return True
        return False

    @staticmethod
    def _matches_entity_list(
        lead: InternalLeadRecord,
        entries: List[Any],
        result: BlockerEvaluationResult,
    ) -> bool:
        """Helper to match a lead against an entity list (referrer or DNC). Delegates to LeadQualifier."""
        from src.lead_qualifier import LeadQualifier
        matched, reason = LeadQualifier._match_entity_entries(lead, entries, "Entity List")
        if matched:
            result.exclusion_reason = reason
            return True
        return False

