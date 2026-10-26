#!/usr/bin/env python3
"""Dental Lead Generator — Endpoint Demo with ZIP Code Only.

Usage:
    python scripts/demo_endpoint.py [ZIP_CODE]

Example:
    python scripts/demo_endpoint.py 91406
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_ZIP = "91406"  # Approved territory in Airtable with fresh leads
SERVER_URL = "http://127.0.0.1:8000"


def run_demo(zip_code: str):
    print("=" * 80)
    print(f"🦷 Dental Lead Generator — Endpoint Demo (ZIP Code Only: '{zip_code}')")
    print("=" * 80)

    # 1. Check if server is running; if not, invoke pipeline directly
    server_online = False
    try:
        r = requests.get(f"{SERVER_URL}/api/health", timeout=3)
        if r.status_code == 200:
            server_online = True
    except Exception:
        server_online = False

    payload = {"zip": zip_code}
    print(f"📤 Sending request payload (ZIP only): {json.dumps(payload)}")

    if server_online:
        print(f"🌐 HTTP Endpoint: POST {SERVER_URL}/api/pipeline/run")
        print("⏳ Waiting for Apify Google Maps Actor and Airtable Ingestion...")
        res = requests.post(
            f"{SERVER_URL}/api/pipeline/run",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=600,
        )
        if res.status_code != 200:
            print(f"❌ Error from server ({res.status_code}): {res.text}")
            return
        results = res.json()
    else:
        print("ℹ️ Server not running on :8000, running pipeline in-process...")
        from config import CampaignType, Config, ExecutionMode
        from src.config_loader import ConfigLoader
        from src.pipeline import LeadPipeline

        loader = ConfigLoader()
        bundle = loader.load_all()
        pipeline = LeadPipeline(config_bundle=bundle)
        results = pipeline.run_discovery_and_process(
            campaign_type=CampaignType.PRM,
            location=zip_code,
            search_scope="zip",
            override_execution_mode=Config.EXECUTION_MODE,
        )

    summary = results.get("summary", {})
    leads = results.get("leads", [])
    ingested_records = results.get("ingested_records", [])

    print("\n" + "=" * 80)
    print("📊 EXECUTION SUMMARY")
    print("=" * 80)
    print(f"  • Execution Mode:              {results.get('execution_mode')}")
    print(f"  • Target ZIP Code:             {zip_code}")
    print(f"  • Total Discovered ('Got'):    {summary.get('total_discovered', len(leads))}")
    print(f"  • Qualified (Eligible):        {summary.get('qualified', 0)}")
    print(f"  • Needs Review:                {summary.get('needs_review', 0)}")
    print(f"  • Duplicates Detected:         {summary.get('duplicates', 0)}")
    print(f"  • Existing Referrer / DNC:     {summary.get('existing_referrer_or_dnc', 0)}")
    print(f"  • Ingested into Airtable:      {results.get('ingested_count', len(ingested_records))}")

    # Display Discovered ("Got") Leads
    print("\n" + "=" * 80)
    print(f"📥 GOT LEADS FROM APIFY ACTOR ({len(leads)} leads)")
    print("=" * 80)
    if leads:
        for idx, l in enumerate(leads, 1):
            pname = l.get("practice_name") or "Unknown"
            phone = l.get("phone") or "N/A"
            addr = l.get("address") or "N/A"
            web = l.get("website") or "N/A"
            status = l.get("qualification_status") or "Unknown"
            reason = l.get("qualification_reason") or "N/A"
            status_symbol = "✅" if status == "Qualified" else ("⚠️" if status == "Needs Review" else "🚫")

            print(f"\n  [{idx}] {pname}")
            print(f"      {status_symbol} Qualification: {status}")
            print(f"      📝 Reason:        {reason}")
            print(f"      📞 Phone:         {phone}")
            print(f"      📍 Address:       {addr}")
            print(f"      🌐 Website:       {web}")
            if l.get("specialist_keywords"):
                print(f"      🔍 Keywords:      {', '.join(l['specialist_keywords'])}")
    else:
        print("  No leads were returned by Apify.")

    # Display Ingested Leads in Airtable
    print("\n" + "=" * 80)
    print(f"🚀 INGESTED LEADS IN AIRTABLE ({len(ingested_records)} records created)")
    print("=" * 80)
    if ingested_records:
        for idx, rec in enumerate(ingested_records, 1):
            rid = rec.get("id")
            fields = rec.get("fields", {})
            pname = fields.get("Practice Name") or fields.get("fldJdczwAbnLj0Xbo") or "Unknown"
            zipc = fields.get("Zipcode") or fields.get("fld4luux8OT8Y7nzx") or zip_code
            ptype = fields.get("Practice Type") or fields.get("fldC5eQbXfAU5Mpz4") or "General Dentist"
            ltype = fields.get("Lead Type") or fields.get("fldChHcXBg1GNId3S") or "Standard PRM"
            qstatus = fields.get("Qualified?") or fields.get("fldhFjad7nSaIrMS0") or "Manually Qualified"

            print(f"\n  [{idx}] Record ID:     {rid}")
            print(f"      🏥 Practice Name: {pname}")
            print(f"      📮 ZIP Code:      {zipc}")
            print(f"      📋 Practice Type: {ptype}")
            print(f"      🏷️ Lead Type:     {ltype}")
            print(f"      ⭐ Airtable Status: {qstatus}")
    else:
        print("  0 records ingested into Airtable.")
        if results.get("execution_mode") == "dry_run":
            print("  (Running in dry_run mode. Set EXECUTION_MODE=live_ingestion and ENABLE_LIVE_INGESTION=true to write to Airtable)")
        else:
            print("  (All discovered leads were either duplicates of existing Airtable practices or flagged for review)")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    zip_arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ZIP
    run_demo(zip_arg)
