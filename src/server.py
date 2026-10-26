"""HTTP API server for Dental Lead Generator.

Provides lightweight, dependency-free REST endpoints using Python's standard library `http.server`:
- GET  /api/health            - Health check, mode status, configuration status
- GET  /api/config            - Current active configuration bundle summary (no secrets exposed)
- POST /api/config/reload     - Reload all 7 JSON configuration files dynamically
- GET  /api/blockers          - List registered blockers and their enable/disable status
- POST /api/pipeline/preview  - Dry-run qualification & blocker evaluation preview for provided lead records
- POST /api/pipeline/run      - Execute pipeline discovery across campaign & location
- PUT|POST /api/config/<resource>, POST /api/config/preview, POST /api/config/territories/approve
- POST /api/zips              - Register candidate ZIP codes
- POST /api/runs              - Start a discovery run (portal flow; honours optional `mode`)
- GET  /api/runs, /api/runs/<id> - Run history / run status
- GET  /api/review            - Pending human-review leads
- POST /api/review/<id>/approve|reject - Resolve a review lead
- GET  /portal                - Static review dashboard
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import CampaignType, Config, ExecutionMode
from src.config_loader import ConfigLoader, ConfigValidationError
from src.lead_normalizer import InternalLeadRecord
from src.pipeline import LeadPipeline

logger = logging.getLogger(__name__)


class DentalLeadApiHandler(BaseHTTPRequestHandler):
    """Request handler implementing the Dental Lead Generator REST API."""

    server_version = "DentalLeadAPI/1.0"

    def _send_json_response(self, status: int, data: Dict[str, Any]) -> None:
        """Helper to send JSON response with appropriate headers."""
        try:
            payload = json.dumps(data, indent=2).encode("utf-8")
        except Exception as e:
            logger.error(f"Failed to serialize JSON response: {e}")
            payload = json.dumps({"error": "Internal serialization error"}).encode("utf-8")
            status = HTTPStatus.INTERNAL_SERVER_ERROR

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        """Safely parse JSON request body."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                return {}
            body = self.rfile.read(content_length).decode("utf-8")
            return json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Invalid JSON in request body", "details": str(e)},
            )
            return None
        except Exception as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Failed to read request body", "details": str(e)},
            )
            return None

    def do_GET(self) -> None:
        """Route GET requests."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")
        if not path:
            path = "/"

        if path in ("/api/health", "/"):
            self.handle_health()
        elif path in ("/portal", "/portal/index.html"):
            self.handle_portal()
        elif path == "/api/config":
            self.handle_get_config()
        elif path.startswith("/api/config/"):
            resource_name = path[len("/api/config/"):]
            self.handle_get_resource(resource_name)
        elif path == "/api/blockers":
            self.handle_get_blockers()
        elif path == "/api/review":
            self.handle_list_review()
        elif path.startswith("/api/runs/"):
            run_id = path[len("/api/runs/"):]
            self.handle_get_run(run_id)
        elif path == "/api/runs":
            self.handle_list_runs()
        else:
            self._send_json_response(
                HTTPStatus.NOT_FOUND,
                {"error": f"Endpoint not found: GET {path}"},
            )

    def do_POST(self) -> None:
        """Route POST requests."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        if path == "/api/config/reload":
            self.handle_reload_config()
        elif path == "/api/config/preview":
            self.handle_preview_config()
        elif path == "/api/config/territories/approve":
            self.handle_approve_territory()
        elif path.startswith("/api/config/"):
            resource_name = path[len("/api/config/"):]
            self.handle_update_resource(resource_name)
        elif path == "/api/pipeline/preview":
            self.handle_pipeline_preview()
        elif path == "/api/pipeline/run":
            self.handle_pipeline_run()
        elif path == "/api/zips":
            self.handle_add_zips()
        elif path == "/api/runs":
            self.handle_start_run()
        elif path.startswith("/api/review/") and path.endswith("/approve"):
            lead_id = path[len("/api/review/"): -len("/approve")]
            self.handle_review_action(lead_id, action="approve")
        elif path.startswith("/api/review/") and path.endswith("/reject"):
            lead_id = path[len("/api/review/"): -len("/reject")]
            self.handle_review_action(lead_id, action="reject")
        else:
            self._send_json_response(
                HTTPStatus.NOT_FOUND,
                {"error": f"Endpoint not found: POST {path}"},
            )

    def do_PUT(self) -> None:
        """Route PUT requests (maps to update resource)."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        if path.startswith("/api/config/"):
            resource_name = path[len("/api/config/"):]
            self.handle_update_resource(resource_name)
        else:
            self._send_json_response(
                HTTPStatus.NOT_FOUND,
                {"error": f"Endpoint not found: PUT {path}"},
            )

    # -------------------------------------------------------------------------
    # Route Handlers
    # -------------------------------------------------------------------------

    def handle_health(self) -> None:
        """GET /api/health - Health check and runtime configuration status."""
        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()
            config_status = "valid"
            config_error = None
        except ConfigValidationError as e:
            config_status = "invalid"
            config_error = str(e)

        data = {
            "status": "healthy",
            "service": "dental-lead-generator",
            "execution_mode": Config.EXECUTION_MODE.value,
            "enable_live_ingestion": Config.ENABLE_LIVE_INGESTION,
            "apify_actor_id": Config.APIFY_ACTOR_ID,
            "apify_configured": bool(Config.APIFY_API_TOKEN),
            "airtable_configured": bool(Config.AIRTABLE_API_KEY and Config.AIRTABLE_BASE_ID),
            "config_status": config_status,
            "config_error": config_error,
        }
        self._send_json_response(HTTPStatus.OK, data)

    def handle_get_config(self) -> None:
        """GET /api/config - Summary of current loaded JSON configurations."""
        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()

            data = {
                "dso_keywords": {
                    "count": len(bundle.dso_keywords.keywords),
                    "keywords": bundle.dso_keywords.keywords,
                    "action": bundle.dso_keywords.action_on_match,
                },
                "excluded_chains": {
                    "wisdom_sales": bundle.excluded_chains.wisdom_sales,
                },
                "territories": {
                    "require_approved_territory": bundle.territories.require_approved_territory,
                    "approved_zip_codes": bundle.territories.approved_zip_codes,
                    "approved_cities": bundle.territories.approved_cities,
                    "approved_states": bundle.territories.approved_states,
                    "is_configured": bool(
                        bundle.territories.approved_zip_codes
                        or bundle.territories.approved_cities
                        or bundle.territories.approved_states
                    ),
                },
                "existing_referrers": {
                    "total_entries": len(bundle.existing_referrers.entries),
                    "active_entries": sum(1 for e in bundle.existing_referrers.entries if e.active),
                },
                "do_not_contact": {
                    "total_entries": len(bundle.do_not_contact.entries),
                    "active_entries": sum(1 for e in bundle.do_not_contact.entries if e.active),
                },
                "zip_codes": {
                    "total_candidates": len(bundle.zip_codes.zip_codes) if bundle.zip_codes else 0,
                    "state_focus": bundle.zip_codes.state_focus if bundle.zip_codes else None,
                },
                "qualification_rules": {
                    "campaigns": list(bundle.qualification_rules.campaigns.keys()),
                    "required_google_maps_fields": bundle.qualification_rules.required_google_maps_fields,
                },
                "blockers": {
                    "total": len(bundle.blockers.blockers),
                    "enabled": sum(1 for b in bundle.blockers.blockers if b.enabled),
                },
                "available_resources": sorted(list(loader.ALLOWLISTED_RESOURCES.keys())),
            }
            self._send_json_response(HTTPStatus.OK, data)
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Failed to load active configuration", "details": str(e)},
            )

    def handle_reload_config(self) -> None:
        """POST /api/config/reload - Reload and validate all 7 JSON configuration files."""
        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()
            self._send_json_response(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "message": "All JSON configuration files reloaded and validated successfully.",
                    "blockers_loaded": len(bundle.blockers.blockers),
                    "dso_keywords_loaded": len(bundle.dso_keywords.keywords),
                    "zip_codes_loaded": len(bundle.zip_codes.zip_codes) if bundle.zip_codes else 0,
                },
            )
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Configuration reload validation failed", "details": str(e)},
            )

    def handle_get_resource(self, resource_name: str) -> None:
        """GET /api/config/<resource> - Fetch a specific configuration resource."""
        loader = self.get_config_loader()
        if resource_name not in loader.ALLOWLISTED_RESOURCES:
            self._send_json_response(
                HTTPStatus.NOT_FOUND,
                {
                    "error": f"Unknown configuration resource '{resource_name}'",
                    "allowed_resources": sorted(list(loader.ALLOWLISTED_RESOURCES.keys())),
                },
            )
            return

        try:
            raw_data = loader.get_resource_raw(resource_name)
            self._send_json_response(
                HTTPStatus.OK,
                {
                    "resource": resource_name,
                    "data": raw_data,
                },
            )
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"Failed to read resource '{resource_name}'", "details": str(e)},
            )

    def handle_update_resource(self, resource_name: str) -> None:
        """POST / PUT /api/config/<resource> - Update a specific configuration resource with validation."""
        loader = self.get_config_loader()
        if resource_name not in loader.ALLOWLISTED_RESOURCES:
            self._send_json_response(
                HTTPStatus.NOT_FOUND,
                {
                    "error": f"Unknown configuration resource '{resource_name}'",
                    "allowed_resources": sorted(list(loader.ALLOWLISTED_RESOURCES.keys())),
                },
            )
            return

        body = self._read_json_body()
        if body is None:
            return

        # Special safety guard: candidate ZIP updates must NEVER automatically approve territories
        if resource_name == "zip_codes":
            # Disallow marking entries as 'approved' through candidate update endpoint
            zip_entries = body.get("zip_codes", [])
            if any(str(z.get("status", "")).lower() == "approved" for z in zip_entries):
                self._send_json_response(
                    HTTPStatus.FORBIDDEN,
                    {
                        "error": "Candidate ZIP codes cannot be directly marked 'approved' via this endpoint.",
                        "details": "To approve territories, use POST /api/config/territories/approve with explicit business approval intent.",
                    },
                )
                return

        # Special safety guard: Updating territories requires explicit approval confirmation if approving
        if resource_name == "territories":
            approved_zips = body.get("approved_zip_codes", [])
            approved_cities = body.get("approved_cities", [])
            approved_states = body.get("approved_states", [])
            if (approved_zips or approved_cities or approved_states) and not body.get("explicit_approval_intent"):
                self._send_json_response(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "Approval of production territories requires explicit confirmation",
                        "details": "Set 'explicit_approval_intent: true' in request payload to confirm intentional territory expansion.",
                    },
                )
                return

        # Clean non-model payload keys before saving
        data_to_save = dict(body)
        data_to_save.pop("explicit_approval_intent", None)

        try:
            loader.save_resource_data(resource_name, data_to_save)
            self._send_json_response(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "resource": resource_name,
                    "message": f"Configuration resource '{resource_name}' validated and persisted successfully.",
                },
            )
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": f"Validation failed for resource '{resource_name}'", "details": str(e)},
            )

    def handle_preview_config(self) -> None:
        """POST /api/config/preview - Validate proposed configuration changes without saving."""
        body = self._read_json_body()
        if body is None:
            return

        resource_name = body.get("resource")
        proposed_data = body.get("data")

        if not resource_name or proposed_data is None:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Fields 'resource' (string) and 'data' (object) are required."},
            )
            return

        loader = self.get_config_loader()
        if resource_name not in loader.ALLOWLISTED_RESOURCES:
            self._send_json_response(
                HTTPStatus.NOT_FOUND,
                {
                    "error": f"Unknown configuration resource '{resource_name}'",
                    "allowed_resources": sorted(list(loader.ALLOWLISTED_RESOURCES.keys())),
                },
            )
            return

        try:
            validated = loader.validate_resource_data(resource_name, proposed_data)
            self._send_json_response(
                HTTPStatus.OK,
                {
                    "status": "valid",
                    "resource": resource_name,
                    "preview": validated.model_dump(),
                    "message": f"Proposed configuration for '{resource_name}' is valid. Changes were NOT saved.",
                },
            )
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {
                    "status": "invalid",
                    "resource": resource_name,
                    "error": "Proposed configuration failed validation",
                    "details": str(e),
                },
            )

    def handle_approve_territory(self) -> None:
        """POST /api/config/territories/approve - Deliberate promotion of candidate ZIPs to approved."""
        body = self._read_json_body()
        if body is None:
            return

        zip_codes = body.get("zip_codes", [])
        confirm_intent = body.get("confirm_approval", False)

        if not confirm_intent:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "Explicit territory approval confirmation required",
                    "details": "Must include 'confirm_approval: true' to approve production territory.",
                },
            )
            return

        if not isinstance(zip_codes, list) or not zip_codes:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Field 'zip_codes' must be a non-empty list of string ZIP codes to approve."},
            )
            return

        # Ensure all are strings
        for z in zip_codes:
            if not isinstance(z, str):
                self._send_json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"Invalid zip code '{z}'. All ZIP codes must be strings to preserve leading zeros."},
                )
                return

        loader = self.get_config_loader()
        try:
            # Load existing territories
            terr_cfg = loader.load_territories()
            current_approved = set(terr_cfg.approved_zip_codes)
            for z in zip_codes:
                current_approved.add(str(z).strip()[:5])

            terr_data = {
                "version": terr_cfg.version,
                "description": terr_cfg.description,
                "approved_zip_codes": sorted(list(current_approved)),
                "approved_cities": terr_cfg.approved_cities,
                "approved_states": terr_cfg.approved_states,
                "require_approved_territory": terr_cfg.require_approved_territory,
            }

            loader.save_resource_data("territories", terr_data)
            self._send_json_response(
                HTTPStatus.OK,
                {
                    "status": "success",
                    "message": f"Successfully approved {len(zip_codes)} ZIP code(s) for live production ingestion.",
                    "newly_approved": zip_codes,
                    "total_approved_zip_codes": len(current_approved),
                },
            )
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Failed to update territories", "details": str(e)},
            )

    def handle_get_blockers(self) -> None:
        """GET /api/blockers - List registered blocker definitions and their toggle states."""
        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()
            blockers_list = [b.model_dump() for b in bundle.blockers.blockers]
            self._send_json_response(
                HTTPStatus.OK,
                {
                    "total": len(blockers_list),
                    "blockers": blockers_list,
                },
            )
        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Failed to load blockers", "details": str(e)},
            )

    def handle_pipeline_preview(self) -> None:
        """POST /api/pipeline/preview - Offline preview of qualification & blocker evaluation.

        Safe testing endpoint: Takes mock or raw leads in the body and evaluates qualification,
        deduplication, and blockers WITHOUT scraping or writing to Airtable.
        """
        body = self._read_json_body()
        if body is None:
            return

        campaign_str = body.get("campaign", "PRM")
        raw_leads = body.get("leads", [])

        if not isinstance(raw_leads, list):
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Field 'leads' must be a list of lead objects."},
            )
            return

        try:
            campaign_type = CampaignType(campaign_str)
        except ValueError:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": f"Invalid campaign: '{campaign_str}'. Allowed: {[c.value for c in CampaignType]}",
                },
            )
            return

        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()
            pipeline = LeadPipeline(config_bundle=bundle, offline=True)

            # Normalize, deduplicate, qualify, and evaluate blockers
            normalized_leads: List[InternalLeadRecord] = []
            for item in raw_leads:
                lead = pipeline.normalizer.normalize_lead(
                    raw_data=item,
                    search_keyword=item.get("search_keyword", "General dentist"),
                    campaign_type=campaign_type.value,
                )
                normalized_leads.append(lead)

            deduped = pipeline.deduplicator.deduplicate_batch(normalized_leads)

            results: List[Dict[str, Any]] = []
            blocker_counts: Dict[str, int] = {}
            eligible_count = 0

            for lead in deduped:
                pipeline.qualifier.qualify(lead)
                eval_res = pipeline.blocker_engine.evaluate_lead(lead)
                mandatory_pass = pipeline._enforce_mandatory_safety(lead, eval_res)
                is_permitted = eval_res.ingestion_permitted and mandatory_pass

                if is_permitted:
                    eligible_count += 1

                for b_id in eval_res.triggered_blocker_ids:
                    blocker_counts[b_id] = blocker_counts.get(b_id, 0) + 1

                lead_dict = lead.model_dump()
                lead_dict["triggered_blocker_ids"] = eval_res.triggered_blocker_ids
                lead_dict["blocker_messages"] = eval_res.blocker_messages
                lead_dict["matched_dso_keyword"] = eval_res.matched_dso_keyword
                lead_dict["duplicate_match_reason"] = eval_res.duplicate_match_reason
                lead_dict["ingestion_permitted"] = is_permitted
                results.append(lead_dict)

            response_data = {
                "campaign": campaign_type.value,
                "total_evaluated": len(results),
                "eligible_for_ingestion": eligible_count,
                "blocker_counts": blocker_counts,
                "leads": results,
            }
            self._send_json_response(HTTPStatus.OK, response_data)

        except Exception as e:
            logger.error(f"Preview evaluation failed: {e}", exc_info=True)
            self._send_json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Preview evaluation error", "details": str(e)},
            )

    def handle_pipeline_run(self) -> None:
        """POST /api/pipeline/run - Execute pipeline discovery.

        Side-effect notice: Invokes discovery via Apify if token is configured.
        Respects EXECUTION_MODE (defaults to dry_run; live ingestion requires explicit authorization).
        """
        body = self._read_json_body()
        if body is None:
            return

        campaign_str = body.get("campaign", "PRM")
        location = body.get("location") or body.get("zip") or body.get("zip_code") or ""
        keywords = body.get("keywords")
        max_results = body.get("max_results")
        mode_override = body.get("mode")
        search_scope = body.get("search_scope", "zip")

        try:
            campaign_type = CampaignType(campaign_str)
        except ValueError:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": f"Invalid campaign: '{campaign_str}'. Allowed: {[c.value for c in CampaignType]}",
                },
            )
            return

        execution_mode = None
        if mode_override:
            try:
                execution_mode = ExecutionMode(mode_override.lower())
            except ValueError:
                self._send_json_response(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": f"Invalid mode: '{mode_override}'. Allowed: {[m.value for m in ExecutionMode]}",
                    },
                )
                return

        # Refuse unauthorized live ingestion requests
        if execution_mode == ExecutionMode.LIVE_INGESTION and not Config.is_live_ingestion_authorized():
            self._send_json_response(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "Live ingestion is not authorized",
                    "details": "ENABLE_LIVE_INGESTION must be set to 'true' in environment and complete Airtable credentials must be provided.",
                },
            )
            return

        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()
            pipeline = LeadPipeline(config_bundle=bundle)

            results = pipeline.run_discovery_and_process(
                campaign_type=campaign_type,
                location=location,
                search_scope=search_scope,
                keywords=keywords,
                max_results_per_keyword=max_results,
                override_execution_mode=execution_mode,
            )
            self._send_json_response(HTTPStatus.OK, results)

        except ConfigValidationError as e:
            self._send_json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": "Configuration error", "details": str(e)},
            )
        except Exception as e:
            logger.error(f"Pipeline execution failed: {e}", exc_info=True)
            self._send_json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Pipeline execution failed", "details": str(e)},
            )

    # -------------------------------------------------------------------------
    # Review Queue & Run Management Handlers
    # -------------------------------------------------------------------------

    def get_database(self) -> Any:
        from src.db import LeadDatabase
        return LeadDatabase()

    def handle_list_review(self) -> None:
        """GET /api/review - Returns pending leads in review queue."""
        db = self.get_database()
        leads = db.list_review_leads(status="pending")
        self._send_json_response(HTTPStatus.OK, {"total": len(leads), "leads": leads})

    def handle_review_action(self, lead_id: str, action: str) -> None:
        """POST /api/review/<id>/approve|reject - Approve or reject escalated lead."""
        db = self.get_database()
        lead_record = db.get_review_lead(lead_id)
        if not lead_record:
            self._send_json_response(HTTPStatus.NOT_FOUND, {"error": f"Review lead '{lead_id}' not found."})
            return

        if action == "reject":
            db.update_review_lead_status(lead_id, "rejected")
            self._send_json_response(HTTPStatus.OK, {"status": "success", "lead_id": lead_id, "action": "rejected"})
            return

        # Approve action: Create in Airtable if not duplicate
        raw_lead = lead_record.get("raw_data") or {}
        loader = self.get_config_loader()
        bundle = loader.load_all()
        pipeline = LeadPipeline(config_bundle=bundle)

        try:
            internal_lead = InternalLeadRecord(**raw_lead) if raw_lead else None
        except Exception as e:
            self._send_json_response(HTTPStatus.BAD_REQUEST, {"error": "Stored lead data is invalid.", "details": str(e)})
            return
        if not internal_lead:
            self._send_json_response(HTTPStatus.BAD_REQUEST, {"error": "Missing raw lead data to approve."})
            return

        internal_lead.qualification_status = "Qualified"
        internal_lead.qualification_reason = "Manually Approved via Portal"

        # Check deduplication before write
        deduped = pipeline.deduplicator.evaluate_lead(internal_lead)
        if deduped.duplicate_status != "Confirmed Duplicate":
            # Human approval overrides soft (Potential/Different Location) flags that downgrade status
            internal_lead.qualification_status = "Qualified"
            internal_lead.qualification_reason = "Manually Approved via Portal"
        if deduped.duplicate_status == "Confirmed Duplicate":
            db.update_review_lead_status(lead_id, "duplicate")
            self._send_json_response(
                HTTPStatus.CONFLICT,
                {"error": "Cannot approve lead: Confirmed duplicate of an existing record.", "details": deduped.qualification_reason},
            )
            return

        created = pipeline.airtable_client.create_lead_records([internal_lead], allow_ingestion=Config.ENABLE_LIVE_INGESTION)
        db.update_review_lead_status(lead_id, "approved")
        self._send_json_response(
            HTTPStatus.OK,
            {
                "status": "success",
                "lead_id": lead_id,
                "action": "approved",
                "airtable_created": len(created) > 0,
                "records": created,
            },
        )

    def handle_add_zips(self) -> None:
        """POST /api/zips - Add candidate ZIPs or register ZIPs."""
        body = self._read_json_body()
        if body is None:
            return
        zips = body.get("zips") or body.get("zip_codes") or []
        if not isinstance(zips, list) or not zips:
            self._send_json_response(HTTPStatus.BAD_REQUEST, {"error": "Field 'zips' must be a non-empty list."})
            return

        loader = self.get_config_loader()
        zip_cfg = loader.load_zip_codes()
        existing = {z.zip_code for z in zip_cfg.zip_codes}
        added = []
        for z in zips:
            z_str = str(z).strip()
            if not (z_str.isdigit() and len(z_str) == 5):
                self._send_json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"Invalid ZIP '{z_str}'. ZIP codes must be 5-digit strings."},
                )
                return
            if z_str not in existing:
                from src.config_loader import ZipCodeEntry
                zip_cfg.zip_codes.append(ZipCodeEntry(zip_code=z_str, status="candidate"))
                existing.add(z_str)
                added.append(z_str)

        loader.save_resource_data("zip_codes", zip_cfg.model_dump())
        self._send_json_response(HTTPStatus.OK, {"status": "success", "added_zips": added, "total": len(zip_cfg.zip_codes)})

    def handle_start_run(self) -> None:
        """POST /api/runs - Start a discovery run across given ZIP codes or location."""
        body = self._read_json_body()
        if body is None:
            return
        campaign_str = body.get("campaign", "PRM")
        location = body.get("location") or body.get("zip") or body.get("zip_code")
        scope = body.get("search_scope", "zip")
        max_results = body.get("max_results", 5)
        keywords = body.get("keywords")
        mode_override = body.get("mode")

        try:
            campaign_type = CampaignType(campaign_str)
        except ValueError:
            self._send_json_response(HTTPStatus.BAD_REQUEST, {"error": f"Invalid campaign '{campaign_str}'"})
            return

        execution_mode = None
        if mode_override:
            try:
                execution_mode = ExecutionMode(str(mode_override).lower())
            except ValueError:
                self._send_json_response(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"Invalid mode: '{mode_override}'. Allowed: {[m.value for m in ExecutionMode]}"},
                )
                return

        if execution_mode == ExecutionMode.LIVE_INGESTION and not Config.is_live_ingestion_authorized():
            self._send_json_response(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "Live ingestion is not authorized",
                    "details": "ENABLE_LIVE_INGESTION must be set to 'true' in environment and complete Airtable credentials must be provided.",
                },
            )
            return

        try:
            loader = self.get_config_loader()
            bundle = loader.load_all()
            pipeline = LeadPipeline(config_bundle=bundle)
            results = pipeline.run_discovery_and_process(
                campaign_type=campaign_type,
                location=str(location),
                search_scope=scope,
                keywords=keywords,
                max_results_per_keyword=max_results,
                override_execution_mode=execution_mode,
            )
            self._send_json_response(HTTPStatus.OK, results)
        except ConfigValidationError as e:
            self._send_json_response(HTTPStatus.BAD_REQUEST, {"error": "Configuration error", "details": str(e)})
        except Exception as e:
            logger.error(f"Run failed: {e}", exc_info=True)
            self._send_json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Run failed", "details": str(e)})

    def handle_get_run(self, run_id: str) -> None:
        """GET /api/runs/<id> - Status and metrics of run."""
        db = self.get_database()
        run = db.get_run(run_id)
        if not run:
            self._send_json_response(HTTPStatus.NOT_FOUND, {"error": f"Run '{run_id}' not found."})
            return
        self._send_json_response(HTTPStatus.OK, run)

    def handle_list_runs(self) -> None:
        """GET /api/runs - List recent runs."""
        db = self.get_database()
        runs = db.list_runs()
        errors = db.list_search_errors()
        self._send_json_response(HTTPStatus.OK, {"total": len(runs), "runs": runs, "recent_errors": errors})

    def handle_portal(self) -> None:
        """GET /portal - Serve static single-page review dashboard."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Wizdom Dental Lead Review Portal</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px; background: #f8fafc; color: #1e293b; }
    h1, h2 { color: #0f172a; margin-top: 0; }
    .card { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; }
    .btn { background: #2563eb; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: 500; }
    .btn-green { background: #16a34a; }
    .btn-red { background: #dc2626; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
    th { background: #f1f5f9; color: #475569; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; background: #fef3c7; color: #92400e; }
    input[type="text"] { padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 4px; width: 280px; }
  </style>
</head>
<body>
  <h1>Wizdom Dental Lead Escalation Portal</h1>
  <div class="card">
    <h2>1. Launch Discovery or Add ZIP</h2>
    <div style="display: flex; gap: 12px; align-items: center;">
      <input type="text" id="targetZip" placeholder="e.g. 91423 or Austin, TX">
      <select id="campaignSelect" style="padding: 8px; border-radius: 4px; border: 1px solid #cbd5e1;">
        <option value="PRM">PRM (General Dentists)</option>
        <option value="WISDOM_SALES">Wisdom Sales (Specialists)</option>
      </select>
      <button class="btn" onclick="startRun()">Start Discovery Run</button>
      <button class="btn" style="background: #475569;" onclick="addZip()">Register Candidate ZIP</button>
    </div>
    <div id="runOutput" style="margin-top: 12px; font-size: 14px; color: #64748b;"></div>
  </div>

  <div class="card">
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <h2>2. Leads in Review Queue (Pending Verification)</h2>
      <button class="btn" onclick="loadReviewQueue()">Refresh Queue</button>
    </div>
    <table id="reviewTable">
      <thead>
        <tr>
          <th>Practice Name</th>
          <th>Phone / Address</th>
          <th>Website</th>
          <th>Reason</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="reviewList">
        <tr><td colspan="5" style="text-align: center; color: #94a3b8;">Loading pending leads...</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    async function loadReviewQueue() {
      const res = await fetch('/api/review');
      const data = await res.json();
      const tbody = document.getElementById('reviewList');
      tbody.innerHTML = '';
      if (!data.leads || data.leads.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #64748b;">No pending leads in review queue. Clean!</td></tr>';
        return;
      }
      data.leads.forEach(l => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td><strong>${l.practice_name}</strong><br><span style="font-size: 11px; color: #64748b;">${l.campaign}</span></td>
          <td>${l.phone || 'N/A'}<br><small style="color: #64748b;">${l.address || ''}</small></td>
          <td>${l.website ? `<a href="${l.website}" target="_blank">${l.website}</a>` : 'None'}</td>
          <td><span class="tag">${l.qualification_reason || 'Needs Review'}</span></td>
          <td>
            <button class="btn btn-green" onclick="actLead('${l.lead_id}', 'approve')">Approve</button>
            <button class="btn btn-red" onclick="actLead('${l.lead_id}', 'reject')">Reject</button>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function actLead(leadId, action) {
      const res = await fetch(`/api/review/${leadId}/${action}`, { method: 'POST' });
      const data = await res.json();
      loadReviewQueue();
    }

    async function startRun() {
      const val = document.getElementById('targetZip').value.trim() || '91423';
      const camp = document.getElementById('campaignSelect').value;
      document.getElementById('runOutput').innerText = 'Starting run...';
      const res = await fetch('/api/runs', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ campaign: camp, location: val, search_scope: 'zip', max_results: 5 })
      });
      const data = await res.json();
      document.getElementById('runOutput').innerText = `Run complete: Discovered ${data.summary?.total_discovered || 0}, Qualified: ${data.summary?.qualified || 0}, Needs Review: ${data.summary?.needs_review || 0}`;
      loadReviewQueue();
    }

    async function addZip() {
      const val = document.getElementById('targetZip').value.trim();
      if (!val) return;
      const res = await fetch('/api/zips', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ zips: [val] })
      });
      const data = await res.json();
      alert(`Registered ZIP ${val}. Total: ${data.total}`);
    }

    window.onload = loadReviewQueue;
  </script>
</body>
</html>
"""
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)



    def get_config_loader(self) -> ConfigLoader:
        """Helper to get ConfigLoader with optional custom config_dir from server."""
        config_dir = getattr(self.server, "config_dir", None)
        return ConfigLoader(config_dir=config_dir)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


def create_server(host: str = "127.0.0.1", port: int = 8000, config_dir: Optional[Path] = None) -> HTTPServer:
    """Create and return an HTTPServer instance with optional custom config_dir."""
    server_address = (host, port)
    server = ReusableHTTPServer(server_address, DentalLeadApiHandler)
    if config_dir is not None:
        server.config_dir = Path(config_dir)  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Dental Lead Generator HTTP API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    httpd = create_server(args.host, args.port)
    print(f"🚀 Dental Lead Generator API running at http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping API server...")
        httpd.server_close()