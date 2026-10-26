"""Lead processing pipeline coordinator.

Orchestrates:
1. Validate JSON configuration (all files).
2. Load Airtable approved ZIP codes, DNC list, and existing leads (if configured).
3. Search discovery queries ({keyword} in {zipcode} or {city}).
4. Normalize Google Maps fields.
5. Website & doctor bio qualification (specialist keyword extraction, DSO detection).
6. Deduplicate using client hierarchy (core domain, phone, address, Place ID).
7. Apply campaign qualification rules from JSON.
8. Evaluate configured blockers.
9. Enforce mandatory safety gates.
10. Store Needs Review leads in local SQLite review portal DB.
11. Safe create in Airtable Practice Pipeline only when execution_mode == live_ingestion.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config import CampaignType, Config, ExecutionMode, QualificationStatus
from src.airtable_client import AirtableClient
from src.apify_google_maps import ApifyGoogleMapsClient
from src.blocker_engine import BlockerEngine, BlockerEvaluationResult
from src.config_loader import AppConfigBundle, ConfigLoader, EntityEntry
from src.db import LeadDatabase
from src.deduplicator import Deduplicator
from src.lead_normalizer import InternalLeadRecord, LeadNormalizer
from src.lead_qualifier import LeadQualifier
from src.website_qualifier import WebsiteQualifier

logger = logging.getLogger(__name__)


class LeadPipeline:
    """End-to-end pipeline orchestrating discovery, qualification, and ingestion."""

    DEFAULT_CAMPAIGN_KEYWORDS = {
        CampaignType.PRM: [
            "General dentist",
            "Family dentist",
            "Comprehensive dentist",
        ],
        CampaignType.WISDOM_SALES: [
            "Periodontist",
            "Oral surgeon",
            "Oral and maxillofacial surgeon",
            "Prosthodontist",
            "Jaw surgeon",
        ],
    }

    def __init__(
        self,
        config_bundle: Optional[AppConfigBundle] = None,
        scraper: Optional[ApifyGoogleMapsClient] = None,
        normalizer: Optional[LeadNormalizer] = None,
        qualifier: Optional[LeadQualifier] = None,
        blocker_engine: Optional[BlockerEngine] = None,
        deduplicator: Optional[Deduplicator] = None,
        airtable_client: Optional[AirtableClient] = None,
        website_qualifier: Optional[WebsiteQualifier] = None,
        database: Optional[LeadDatabase] = None,
        offline: bool = False,
    ):
        if config_bundle is None:
            loader = ConfigLoader()
            self.bundle = loader.load_all()
        else:
            self.bundle = config_bundle

        self.offline = offline
        self.scraper = scraper or ApifyGoogleMapsClient()
        self.normalizer = normalizer or LeadNormalizer()
        self.airtable_client = airtable_client or AirtableClient()
        self.website_qualifier = website_qualifier or WebsiteQualifier()
        self.db = database or LeadDatabase()

        # Synchronize approved ZIP codes from Airtable if connected (skip in offline mode)
        if not self.offline:
            self._sync_airtable_territories_and_dnc()

        self.qualifier = qualifier or LeadQualifier(self.bundle)
        self.blocker_engine = blocker_engine or BlockerEngine(self.bundle)

        # Initialize deduplicator with configured conflict rule and existing records
        if deduplicator:
            self.deduplicator = deduplicator
        else:
            existing_records: List[Dict[str, Any]] = []
            self.dedupe_load_error: Optional[str] = None
            if not self.offline:
                try:
                    if self.airtable_client.api_key and self.airtable_client.base_id:
                        existing_raw = self.airtable_client.fetch_all_records(
                            table_id_or_name=self.airtable_client.table_name,
                            fields=["Practice Name", "Phone Number", "Company Website", "Address", "Zipcode"],
                        )
                        existing_records = [
                            {**r.get("fields", {}), "record_id": r.get("id")} for r in existing_raw
                        ]
                except Exception as e:
                    self.dedupe_load_error = str(e)
                    logger.warning(f"Could not fetch existing records from Airtable for deduplication: {e}")

            domain_conflict_rule = self.bundle.qualification_rules.dedupe.domain_match_different_address
            self.deduplicator = Deduplicator(
                existing_records=existing_records,
                domain_match_different_address=domain_conflict_rule,
            )

    def _sync_airtable_territories_and_dnc(self) -> None:
        """Load approved territories and DNC entries from Airtable if available."""
        try:
            if self.airtable_client.api_key and self.airtable_client.base_id:
                # 1. Approved ZIPs from Zipcode List table
                approved_zips, zip_links = self.airtable_client.load_approved_zipcodes()
                if approved_zips:
                    current_approved = set(self.bundle.territories.approved_zip_codes)
                    current_approved.update(approved_zips)
                    self.bundle.territories.approved_zip_codes = sorted(list(current_approved))
                    logger.info(f"Loaded {len(approved_zips)} approved ZIP codes from Airtable Zipcode List.")

                # 2. DNC and existing referrers from CLEAN DNC table
                dnc_records = self.airtable_client.load_dnc_and_referrers()
                if dnc_records:
                    for rec in dnc_records:
                        f = rec.get("fields", {})
                        pname = f.get("Practice Name")
                        if pname:
                            self.bundle.do_not_contact.entries.append(
                                EntityEntry(type="name", value=pname, reason="Airtable DNC List")
                            )
                        cphone = f.get("Cell Phone")
                        if cphone:
                            self.bundle.do_not_contact.entries.append(
                                EntityEntry(type="phone", value=cphone, reason="Airtable DNC Cell")
                            )
                        ophone = f.get("Office Phone")
                        if ophone:
                            self.bundle.do_not_contact.entries.append(
                                EntityEntry(type="phone", value=ophone, reason="Airtable DNC Office Phone")
                            )
                        web = f.get("Website")
                        if web:
                            self.bundle.do_not_contact.entries.append(
                                EntityEntry(type="domain", value=web, reason="Airtable DNC Website")
                            )
        except Exception as e:
            logger.warning(f"Airtable synchronization skipped: {e}")

    def run_discovery_and_process(
        self,
        campaign_type: CampaignType,
        location: Optional[str] = None,
        search_scope: str = "zip",
        keywords: Optional[List[str]] = None,
        max_results_per_keyword: Optional[int] = None,
        override_execution_mode: Optional[ExecutionMode] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute discovery across specified campaign keywords and territory."""
        mode = override_execution_mode or Config.EXECUTION_MODE
        # Fail closed: never ingest when the Airtable duplicate snapshot could not be loaded.
        if mode == ExecutionMode.LIVE_INGESTION and getattr(self, "dedupe_load_error", None):
            raise RuntimeError(
                "Cannot run live ingestion: existing Airtable records could not be loaded for "
                f"duplicate checking ({self.dedupe_load_error})"
            )
        target_val = location or "default"
        rid = self.db.create_run(campaign=campaign_type.value, search_scope=search_scope, target_value=target_val, run_id=run_id)

        # Keywords per campaign
        camp_rule = self.bundle.qualification_rules.campaigns.get(campaign_type.value)
        target_keywords = keywords or (
            camp_rule.target_categories if camp_rule and camp_rule.target_categories
            else self.DEFAULT_CAMPAIGN_KEYWORDS.get(campaign_type, [])
        )

        all_normalized_leads: List[InternalLeadRecord] = []

        logger.info(
            f"Starting pipeline run {rid}: Campaign={campaign_type.value}, Mode={mode.value}, "
            f"Scope={search_scope}, Location={location}"
        )

        # Scrape & Normalize
        for kw in target_keywords:
            query = f"{kw} in {location}" if location else kw
            try:
                raw_items = self.scraper.search(
                    keyword=query,
                    max_results=max_results_per_keyword,
                )
                for item in raw_items:
                    normalized = self.normalizer.normalize_lead(
                        raw_data=item,
                        search_keyword=kw,
                        campaign_type=campaign_type.value,
                    )
                    all_normalized_leads.append(normalized)
            except Exception as e:
                logger.error(f"Apify Actor search failed for '{query}': {e}")
                self.db.record_search_error(location or "", kw, str(e), run_id=rid)

        # Website & Doctor Bio Qualification
        for lead in all_normalized_leads:
            try:
                self.website_qualifier.qualify_lead_website(lead)
            except Exception as e:
                logger.debug(f"Website qualification skipped for {lead.practice_name}: {e}")

        # Deduplicate
        deduped_leads = self.deduplicator.deduplicate_batch(all_normalized_leads)

        # Qualification & Blocker Engine
        processed_leads_data: List[Dict[str, Any]] = []
        eligible_for_ingestion: List[InternalLeadRecord] = []
        blocker_counts: Dict[str, int] = {}

        for lead in deduped_leads:
            self.qualifier.qualify(lead)
            if lead.qualification_reason and lead.qualification_reason not in lead.qualification_reasons:
                lead.qualification_reasons.append(lead.qualification_reason)

            eval_res: BlockerEvaluationResult = self.blocker_engine.evaluate_lead(lead)
            lead.blocker_ids = list(eval_res.triggered_blocker_ids)
            for b_id in eval_res.triggered_blocker_ids:
                blocker_counts[b_id] = blocker_counts.get(b_id, 0) + 1

            mandatory_pass = self._enforce_mandatory_safety(lead, eval_res)
            is_permitted = eval_res.ingestion_permitted and mandatory_pass

            if is_permitted:
                eligible_for_ingestion.append(lead)
            elif lead.qualification_status == QualificationStatus.NEEDS_REVIEW.value:
                # Add to local SQLite Human Review Queue
                self.db.add_review_lead(lead.model_dump(), run_id=rid)

            lead_dump = lead.model_dump()
            lead_dump["triggered_blocker_ids"] = eval_res.triggered_blocker_ids
            lead_dump["blocker_messages"] = eval_res.blocker_messages
            lead_dump["matched_dso_keyword"] = eval_res.matched_dso_keyword
            lead_dump["duplicate_match_reason"] = eval_res.duplicate_match_reason
            lead_dump["ingestion_permitted"] = is_permitted
            processed_leads_data.append(lead_dump)

        # Summary statistics
        summary = {
            "total_discovered": len(processed_leads_data),
            "qualified": sum(1 for l in processed_leads_data if l["qualification_status"] == QualificationStatus.QUALIFIED.value),
            "needs_review": sum(1 for l in processed_leads_data if l["qualification_status"] == QualificationStatus.NEEDS_REVIEW.value),
            "disqualified": sum(1 for l in processed_leads_data if l["qualification_status"] == QualificationStatus.DISQUALIFIED.value),
            "duplicates": sum(1 for l in processed_leads_data if l["qualification_status"] == QualificationStatus.DUPLICATE.value),
            "existing_referrer_or_dnc": sum(
                1 for l in processed_leads_data if l["qualification_status"] == QualificationStatus.EXISTING_REFERRER_OR_DNC.value
            ),
            "eligible_for_ingestion": len(eligible_for_ingestion),
            "blocker_counts": blocker_counts,
            "ingested": 0,
        }

        # Airtable Ingestion (Only in live_ingestion mode with authorized flags)
        ingested_records: List[Dict[str, Any]] = []
        if mode == ExecutionMode.LIVE_INGESTION and Config.is_live_ingestion_authorized():
            ingested_records = self.airtable_client.create_lead_records(
                eligible_for_ingestion,
                allow_ingestion=True,
            )
            summary["ingested"] = len(ingested_records)
            # If location is a ZIP code, optionally increment run count
            if location and location.isdigit() and len(location) == 5:
                self.airtable_client.increment_zip_run_count(location)

        # Update run in local SQLite DB
        self.db.update_run_summary(rid, status="completed", counts=summary)

        return {
            "run_id": rid,
            "execution_mode": mode.value,
            "campaign": campaign_type.value,
            "search_scope": search_scope,
            "summary": summary,
            "leads": processed_leads_data,
            "ingested_count": len(ingested_records),
            "ingested_records": ingested_records,
        }

    @staticmethod
    def _enforce_mandatory_safety(
        lead: InternalLeadRecord,
        eval_res: BlockerEvaluationResult,
    ) -> bool:
        """Enforce mandatory protections that CANNOT be bypassed by disabling blockers."""
        if lead.qualification_status != QualificationStatus.QUALIFIED.value:
            return False
        if lead.duplicate_status == "Confirmed Duplicate":
            return False
        if not lead.practice_name or lead.practice_name.lower() in ("unknown practice", ""):
            return False
        if not lead.phone and not lead.website and not lead.address:
            return False
        if eval_res and eval_res.exclusion_reason:
            return False
        if "candidate" in (lead.qualification_reason or "").lower():
            return False
        return True