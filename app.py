"""CLI Entry point for Dental Lead Generator & Airtable Ingestion.

Allows running dry-run discovery, live discovery, or authorized live ingestion
from the command line with options for campaigns, territories, and limits.
Powered by Apify Google Maps Actor (nwua9Gu5YrADL7ZDj) and human-editable JSON configuration.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List

from config import CampaignType, Config, ExecutionMode
from src.config_loader import ConfigLoader, ConfigValidationError
from src.lead_normalizer import InternalLeadRecord
from src.pipeline import LeadPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dental_lead_generator")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dental Practice Discovery & Airtable Ingestion CLI (Apify Actor nwua9Gu5YrADL7ZDj + JSON Config)"
    )

    parser.add_argument(
        "--campaign",
        choices=[CampaignType.PRM.value, CampaignType.WISDOM_SALES.value],
        default=CampaignType.PRM.value,
        help="Campaign type: PRM (General/Family) or WISDOM_SALES (Specialists)",
    )

    parser.add_argument(
        "--location",
        type=str,
        default="",
        help="City, State, or ZIP code (e.g. 'Austin, TX' or '90210')",
    )

    parser.add_argument(
        "--zip",
        type=str,
        default="",
        help="Single target ZIP code (e.g. '91423')",
    )

    parser.add_argument(
        "--search-scope",
        choices=["zip", "city"],
        default="zip",
        help="Search scope: zip (keyword in zipcode) or city (keyword in city)",
    )

    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Perform read-only preflight schema verification against Airtable and exit (zero writes)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode and print exact would-be Airtable payload (zero writes)",
    )

    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path for saving run results",
    )

    parser.add_argument(
        "--keywords",
        nargs="+",
        default=None,
        help="Custom search keywords (overrides default campaign templates)",
    )

    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Maximum results to fetch per keyword",
    )

    parser.add_argument(
        "--mode",
        choices=[e.value for e in ExecutionMode],
        default=None,
        help="Override execution mode (dry_run, live_discovery, live_ingestion)",
    )

    parser.add_argument(
        "--server",
        action="store_true",
        help="Start the Dental Lead Generator HTTP API server on --host and --port",
    )

    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address for API server (default: 127.0.0.1)",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for API server (default: 8000)",
    )

    args = parser.parse_args()

    # Preflight Check Only
    if args.preflight:
        from src.airtable_client import AirtableClient
        print("=" * 75)
        print("🔍 Airtable Preflight Schema Inspection (Read-Only Meta API)")
        print("=" * 75)
        client = AirtableClient()
        report = client.preflight_schema_check()
        print(json.dumps(report, indent=2))
        if report.get("status") in ("success", "skipped"):
            print("✅ Preflight schema inspection completed successfully.")
            return 0
        else:
            print("❌ Preflight schema inspection encountered discrepancies.")
            return 1

    if args.server:
        from src.server import create_server
        print("=" * 75)
        print("🚀 Starting Dental Lead Generator HTTP API Server")
        print(f"Listening on http://{args.host}:{args.port}")
        print("Endpoints available: GET /api/health, GET /api/config, POST /api/config/reload,")
        print("                    GET /api/blockers, POST /api/pipeline/preview, POST /api/pipeline/run")
        print("=" * 75)
        httpd = create_server(args.host, args.port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")
            httpd.server_close()
        return 0

    campaign = CampaignType(args.campaign)
    mode = ExecutionMode(args.mode) if args.mode else Config.EXECUTION_MODE
    if args.dry_run:
        # --dry-run always wins over --mode and the EXECUTION_MODE env var (zero writes)
        mode = ExecutionMode.DRY_RUN

    # Security check: Mask token if displayed
    token_status = "Configured" if Config.APIFY_API_TOKEN else "Missing / Placeholder"

    print("=" * 75)
    print("🦷 Dental Lead Discovery & Airtable Ingestion (Apify + Editable JSON Config)")
    print("=" * 75)
    print(f"Campaign:        {campaign.value}")
    print(f"Execution Mode:  {mode.value}")
    target_loc = args.zip if args.zip else (args.location or "")

    print(f"Location Target: {target_loc or 'Global / Defined in config/territories.json'}")
    print(f"Search Scope:    {args.search_scope}")
    print(f"Apify Actor ID:  {Config.APIFY_ACTOR_ID}")
    print(f"Apify Token:     {token_status}")
    print(f"Airtable Target: Base={Config.AIRTABLE_BASE_ID or 'Not Set'}, Table={Config.AIRTABLE_TABLE_NAME}")
    print("=" * 75)

    try:
        # Load and validate JSON configuration files
        loader = ConfigLoader()
        config_bundle = loader.load_all()
        print("✅ Loaded all JSON configuration files successfully.")

        pipeline = LeadPipeline(config_bundle=config_bundle)
        results = pipeline.run_discovery_and_process(
            campaign_type=campaign,
            location=target_loc,
            search_scope=args.search_scope,
            keywords=args.keywords,
            max_results_per_keyword=args.max_results,
            override_execution_mode=mode,
        )

        summary = results["summary"]
        print("\n📊 Pipeline Execution Summary:")
        print(f"  • Total Leads Discovered:      {summary['total_discovered']}")
        print(f"  • Qualified (Eligible):        {summary['qualified']}")
        print(f"  • Needs Review:                {summary['needs_review']}")
        print(f"  • Disqualified:                {summary['disqualified']}")
        print(f"  • Duplicates Detected:         {summary.get('duplicates', summary.get('duplicate', 0))}")
        print(f"  • Existing Referrer / DNC:     {summary['existing_referrer_or_dnc']}")
        print(f"  • Ingestion Permitted Leads:   {summary['eligible_for_ingestion']}")
        print(f"  • Records Ingested to Airtable:{results['ingested_count']}")

        # Blocker breakdown
        blocker_counts = summary.get("blocker_counts", {})
        if blocker_counts:
            print("\n🚫 Triggered Blockers Breakdown:")
            for b_id, count in sorted(blocker_counts.items(), key=lambda x: -x[1]):
                print(f"  • {b_id:<26}: {count} leads")
        else:
            print("\n🚫 Triggered Blockers Breakdown: None")

        # Display discovered ("got") leads
        leads = results.get("leads", [])
        if leads:
            print("\n" + "=" * 75)
            print(f"📥 DISCOVERED LEADS FROM APIFY ({len(leads)} Total):")
            print("=" * 75)
            for idx, l in enumerate(leads, 1):
                pname = l.get("practice_name") or "Unknown"
                phone = l.get("phone") or "N/A"
                addr = l.get("address") or "N/A"
                web = l.get("website") or "N/A"
                status = l.get("qualification_status") or "Unknown"
                reason = l.get("qualification_reason") or "N/A"
                print(f"  [{idx}] {pname}")
                print(f"      Status:   {status}")
                print(f"      Reason:   {reason}")
                print(f"      Phone:    {phone}")
                print(f"      Address:  {addr}")
                print(f"      Website:  {web}")
                if l.get("specialist_keywords"):
                    print(f"      Keywords: {', '.join(l['specialist_keywords'])}")

        # Display ingested leads in Airtable
        ingested_records = results.get("ingested_records", [])
        if ingested_records:
            print("\n" + "=" * 75)
            print(f"🚀 INGESTED LEADS IN AIRTABLE ({len(ingested_records)} Created):")
            print("=" * 75)
            for idx, r in enumerate(ingested_records, 1):
                rid = r.get("id")
                fields = r.get("fields", {})
                pname = fields.get("Practice Name") or fields.get("fldJdczwAbnLj0Xbo") or "Unknown"
                zipcode = fields.get("Zipcode") or fields.get("fld4luux8OT8Y7nzx") or target_loc
                ptype = fields.get("Practice Type") or fields.get("fldC5eQbXfAU5Mpz4") or "N/A"
                ltype = fields.get("Lead Type") or fields.get("fldChHcXBg1GNId3S") or "N/A"
                print(f"  [{idx}] Record ID:     {rid}")
                print(f"      Practice Name: {pname}")
                print(f"      ZIP Code:      {zipcode}")
                print(f"      Practice Type: {ptype}")
                print(f"      Lead Type:     {ltype}")
        elif mode == ExecutionMode.LIVE_INGESTION:
            print("\n" + "=" * 75)
            print("🚀 INGESTED LEADS IN AIRTABLE: 0 new records")
            print("=" * 75)
            print("  (Existing records were protected from duplicate creation; non-qualified leads flagged accordingly)")

        if mode == ExecutionMode.DRY_RUN:
            print("\n💡 Running in DRY RUN mode: Zero records were sent to Airtable.")
            print("=" * 75)
            print("📋 WOULD-BE AIRTABLE PAYLOAD PREVIEW (Eligible Qualified Records):")
            eligible_leads = [
                InternalLeadRecord(**l) for l in results["leads"]
                if l.get("ingestion_permitted")
            ]
            if eligible_leads:
                for idx, lead_obj in enumerate(eligible_leads, 1):
                    airtable_payload = pipeline.airtable_client.map_lead_to_airtable_fields(lead_obj)
                    print(f"  [{idx}] Record Payload for '{lead_obj.practice_name}':")
                    print(json.dumps(airtable_payload, indent=4))
            else:
                print("  No leads qualified for automatic Airtable ingestion in this run.")
                print("  Review pending leads in Review Portal at http://127.0.0.1:8000/portal")
            print("=" * 75)


        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)
            print(f"\n💾 Full results saved to: {args.output}")

        return 0

    except ConfigValidationError as e:
        logger.error(f"❌ Configuration Validation Error:\n{e}")
        return 1
    except Exception as e:
        logger.error(f"Fatal execution error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())