"""Tests for deduplication module.

Validates:
- Detection by Google Place ID
- Detection by normalized name and physical address
- Preservation of different locations of the same organization as separate leads
- Handling shared phone numbers without accidental merging
- Checking against existing Airtable records
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DuplicateStatus, QualificationStatus
from src.deduplicator import Deduplicator
from src.lead_normalizer import InternalLeadRecord


def test_duplicate_by_place_id():
    existing = [
        {"place_id": "ChIJN1t_tDeuEmsRUsoyG83frY4", "practice_name": "Bay Dental"}
    ]
    dedup = Deduplicator(existing_records=existing)

    lead = InternalLeadRecord(
        practice_name="Bay Dental Center",
        place_id="ChIJN1t_tDeuEmsRUsoyG83frY4",
        search_keyword="General dentist",
        campaign_type="PRM",
    )

    res = dedup.evaluate_lead(lead)
    assert res.duplicate_status == DuplicateStatus.CONFIRMED_DUPLICATE.value
    assert res.qualification_status == QualificationStatus.DUPLICATE.value


def test_duplicate_by_name_and_address():
    existing = [
        {
            "practice_name": "Apex Dental Partners, LLC",
            "address": "400 Oak Ave, Suite 100",
        }
    ]
    dedup = Deduplicator(existing_records=existing)

    lead = InternalLeadRecord(
        practice_name="Apex Dental Partners",
        address="400 Oak Ave, Suite 100",
        search_keyword="General dentist",
        campaign_type="PRM",
    )

    res = dedup.evaluate_lead(lead)
    assert res.duplicate_status == DuplicateStatus.CONFIRMED_DUPLICATE.value
    assert res.qualification_status == QualificationStatus.DUPLICATE.value


def test_separate_practice_locations_preserved():
    existing = [
        {
            "practice_name": "Summit Dental Care",
            "address": "100 North Way",
            "website": "https://summitdentalcare.com",
        }
    ]
    dedup = Deduplicator(existing_records=existing)

    # Same group, different branch location
    branch_lead = InternalLeadRecord(
        practice_name="Summit Dental Care",
        address="500 South Blvd",
        website="https://summitdentalcare.com/south",
        search_keyword="General dentist",
        campaign_type="PRM",
        qualification_status=QualificationStatus.QUALIFIED.value,
        qualification_reason="PRM target practice",
    )

    res = dedup.evaluate_lead(branch_lead)
    # Must NOT be marked duplicate or disqualified
    assert res.duplicate_status == DuplicateStatus.DIFFERENT_LOCATION.value
    assert res.qualification_status == QualificationStatus.QUALIFIED.value
    assert "Additional branch/location" in res.qualification_reason


def test_shared_phone_does_not_merge():
    existing = [
        {"practice_name": "Dr. Miller DDS", "phone": "(555) 333-4444"}
    ]
    dedup = Deduplicator(existing_records=existing)

    new_lead = InternalLeadRecord(
        practice_name="Pediatric Smiles",
        phone="(555) 333-4444",
        address="999 Wellness Way",
        search_keyword="General dentist",
        campaign_type="PRM",
        qualification_status=QualificationStatus.QUALIFIED.value,
    )

    res = dedup.evaluate_lead(new_lead)
    assert res.duplicate_status == DuplicateStatus.POTENTIAL_DUPLICATE.value
    assert res.qualification_status == QualificationStatus.NEEDS_REVIEW.value
    assert "Shared phone" in res.qualification_reason


def _lead(name, phone, website, address, zip_code):
    from src.lead_normalizer import InternalLeadRecord
    return InternalLeadRecord(
        practice_name=name, phone=phone, website=website, address=address, zip_code=zip_code,
        search_keyword="dentist", campaign_type="PRM",
        qualification_status="Qualified", qualification_reason="ok",
    )


def test_airtable_street_only_address_matches_apify_full_address():
    """Airtable stores 'Address' street-only + 'Zipcode'; Apify returns the full address."""
    existing = [{
        "id": "recAAA", "Practice Name": "Valley Smiles", "Phone Number": "(818) 555-0100",
        "Company Website": "https://www.valleysmiles.com/", "Address": "123 Ventura Blvd", "Zipcode": "91423",
    }]
    dedup = Deduplicator(existing_records=existing)
    lead = _lead("Valley Smiles Dental", "+1 818-555-0100", "https://valleysmiles.com",
                 "123 Ventura Boulevard, Sherman Oaks, CA 91423", "91423")
    dedup.evaluate_lead(lead)
    assert lead.duplicate_status == "Confirmed Duplicate"
    assert lead.qualification_status == "Duplicate"


def test_airtable_full_address_matches_and_different_street_is_unique():
    existing = [{"Practice Name": "Full Addr", "Address": "500 Main St, Sherman Oaks, CA 91423", "Zipcode": "91423"}]
    dedup = Deduplicator(existing_records=existing)
    dup = _lead("Other Name", "", "", "500 Main Street, Sherman Oaks, CA 91423", "91423")
    new = _lead("Brand New", "", "", "900 Oak Ave, Sherman Oaks, CA 91423", "91423")
    dedup.evaluate_lead(dup)
    dedup.evaluate_lead(new)
    assert dup.duplicate_status == "Confirmed Duplicate"
    assert new.duplicate_status == "Unique"


def test_live_ingestion_fails_closed_when_airtable_dedupe_fetch_fails(tmp_path):
    from unittest.mock import MagicMock
    import pytest
    from config import CampaignType, ExecutionMode
    from src.db import LeadDatabase
    from src.pipeline import LeadPipeline

    at = MagicMock()
    at.api_key, at.base_id, at.table_name = "k", "b", "t"
    at.load_approved_zipcodes.return_value = ([], {})
    at.load_dnc_and_referrers.return_value = []
    at.fetch_all_records.side_effect = Exception("422 unknown field")
    scraper = MagicMock()
    pipe = LeadPipeline(airtable_client=at, scraper=scraper, database=LeadDatabase(tmp_path / "t.db"))
    with pytest.raises(RuntimeError, match="duplicate checking"):
        pipe.run_discovery_and_process(CampaignType.PRM, location="91423",
                                       override_execution_mode=ExecutionMode.LIVE_INGESTION)
    scraper.search.assert_not_called()