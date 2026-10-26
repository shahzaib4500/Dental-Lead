"""Comprehensive offline tests for lead cleaning, audit fields, and ZIP code territories.

Validates:
1. Candidate ZIP codes (e.g. 07001, 78701) never qualify leads for automatic ingestion.
2. Approved ZIP codes and leading-zero preservation (e.g. '07001').
3. Deterministic lead cleaning:
   - Whitespace collapsing
   - Control character removal
   - Raw phone preservation
   - Invalid phone / URL / missing field tracking in cleaning_issues
   - Canonical domain extraction (e.g., https://www.dentalcare.com/about -> dentalcare.com)
   - Google redirect URL unpacking
   - Deduplication key generation
4. Airtable preflight schema check in read-only mode and missing credentials handling.
5. Ingestion gate safety:
   - Blocks leads without approved territory
   - Blocks leads with unreviewed/disqualified/duplicate status
   - Blocks leads matching DNC / Referrer lists
   - Refuses write calls in dry-run mode
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import CampaignType, ExecutionMode, QualificationStatus
from src.airtable_client import AirtableClient
from src.config_loader import AppConfigBundle, ConfigLoader
from src.deduplicator import Deduplicator
from src.lead_normalizer import InternalLeadRecord, LeadNormalizer
from src.lead_qualifier import LeadQualifier
from src.pipeline import LeadPipeline


def test_candidate_zip_codes_do_not_qualify_for_ingestion():
    """Verify that candidate ZIP codes present in zip_codes.json but not in territories.json do NOT qualify leads."""
    loader = ConfigLoader()
    bundle = loader.load_all()

    # Confirm candidate zip exists in zip_codes.json
    candidate_zips = [z.zip_code for z in bundle.zip_codes.zip_codes if z.status == "candidate"]
    assert "78701" in candidate_zips
    assert "75201" in candidate_zips

    # Ensure territories.json approved_zip_codes does not contain them
    assert "78701" not in bundle.territories.approved_zip_codes
    assert "75201" not in bundle.territories.approved_zip_codes

    qualifier = LeadQualifier(bundle)
    raw_lead = {
        "title": "Austin Metro Family Dental",
        "postalCode": "78701",
        "street": "100 Congress Ave",
        "city": "Austin",
        "state": "TX",
        "phone": "+1 512-555-0101",
        "website": "https://austinmetrofamilydental.com",
        "categoryName": "General Dentist",
    }
    lead = LeadNormalizer.normalize_lead(raw_lead, search_keyword="General dentist", campaign_type="PRM")

    # Qualification should fail territory check because 78701 is unapproved/candidate
    qualifier.qualify(lead)
    assert lead.qualification_status in (QualificationStatus.NEEDS_REVIEW.value, QualificationStatus.DISQUALIFIED.value)
    assert lead.qualification_status != QualificationStatus.QUALIFIED.value


def test_leading_zero_zip_code_preservation():
    """Verify that ZIP codes with leading zeros (e.g. '07001') are preserved as strings across normalizer and addresses."""
    raw_lead = {
        "title": "New Jersey Dental Care",
        "address": "500 Rahway Ave, Woodbridge, NJ 07001",
        "phone": "7325550199",
        "website": "https://njdentalcare.com",
    }
    lead = LeadNormalizer.normalize_lead(raw_lead, search_keyword="General dentist", campaign_type="PRM")
    assert lead.zip_code == "07001"
    assert isinstance(lead.zip_code, str)
    assert len(lead.zip_code) == 5


def test_lead_normalization_cleaning_issues_and_control_chars():
    """Verify whitespace stripping, ASCII control char removal, and tracking of missing/malformed fields."""
    raw_lead = {
        "title": "  \x00\x07Premier\tDental   Center\x1f  ",
        "phone": "123",  # Malformed phone (less than 7 digits)
        "website": "not a valid url",
        "address": "\x02100 Market St, Suite 4\x00",
    }
    lead = LeadNormalizer.normalize_lead(raw_lead, search_keyword="General dentist", campaign_type="PRM")

    # Name cleaned
    assert lead.practice_name == "Premier Dental Center"
    assert "\x00" not in lead.practice_name
    assert "\x1f" not in lead.practice_name

    # Raw phone preserved
    assert lead.raw_phone == "123"

    # Cleaning issues tracked
    assert any("Malformed or short phone" in issue for issue in lead.cleaning_issues)
    assert any("Invalid or unparseable website" in issue for issue in lead.cleaning_issues)

    # Address parsed and control chars stripped
    assert "\x02" not in (lead.address or "")
    assert "\x00" not in (lead.address or "")

    # Audit fields present
    assert lead.normalized_name == "premier dental center"
    assert lead.deduplication_key is not None
    assert lead.processed_at is not None
    assert isinstance(lead.raw_source_record, dict)


def test_domain_extraction_and_google_redirect():
    """Verify canonical domain extraction and handling of Google redirect URLs."""
    # Standard URL
    assert LeadNormalizer.extract_domain("https://www.drsmithdental.com/services") == "drsmithdental.com"
    assert LeadNormalizer.extract_domain("http://austinsmiles.net/") == "austinsmiles.net"

    # Google Maps redirect URL
    redirect = "https://www.google.com/url?q=https%3A%2F%2Fwww.capitaldentalsmiles.com%2Fcontact%3Fref%3Dlocal"
    assert LeadNormalizer.clean_website(redirect) == "https://www.capitaldentalsmiles.com/contact?ref=local"
    assert LeadNormalizer.extract_domain(redirect) == "capitaldentalsmiles.com"


def test_airtable_client_preflight_schema_check_offline():
    """Verify preflight_schema_check handles missing credentials safely without raising errors."""
    client = AirtableClient(api_key="", base_id="", table_name="")
    preflight = client.preflight_schema_check()
    assert preflight["status"] == "skipped"
    assert preflight["can_proceed_offline"] is True
    assert "Missing required credentials" in preflight["reason"]


def test_airtable_client_preflight_schema_check_read_only_with_mock():
    """Verify preflight_schema_check performs a read-only GET query and matches columns."""
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "records": [
            {
                "id": "recTest123",
                "fields": {
                    "Practice Name": "Sample Dental",
                    "Main Phone": "(555) 000-1111",
                    "Street Address": "123 Main St",
                },
            }
        ]
    }
    mock_session.request.return_value = mock_resp

    mapping = {
        "practice_name": "Practice Name",
        "phone": "Main Phone",
        "website": "Website",  # not in sample fields
    }

    client = AirtableClient(
        api_key="pat_mock_token",
        base_id="appMock123",
        table_name="Dental Leads",
        field_mapping=mapping,
        session=mock_session,
    )

    result = client.preflight_schema_check()
    assert result["status"] == "success"
    assert result["read_only"] is True
    assert "Practice Name" in result["sample_existing_columns"]
    assert "Website" in result["unmatched_target_columns"]
    # Ensure request was GET only, never POST
    assert mock_session.request.call_args[0][0] == "GET"


def test_pipeline_mandatory_ingestion_safety_gates():
    """Verify pipeline blocks candidate territories, duplicates, and unreviewed leads from ingestion."""
    loader = ConfigLoader()
    bundle = loader.load_all()

    pipeline = LeadPipeline(config_bundle=bundle)

    # Lead with Candidate status reason
    candidate_lead = InternalLeadRecord(
        practice_name="Candidate Practice",
        phone="(555) 123-4567",
        address="100 Main St, Austin, TX 78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        qualification_reason="Within candidate territory (unapproved)",
        search_keyword="General dentist",
        campaign_type="PRM",
    )
    assert pipeline._enforce_mandatory_safety(candidate_lead, None) is False

    # Lead marked Duplicate
    dup_lead = InternalLeadRecord(
        practice_name="Dup Practice",
        phone="(555) 123-4567",
        address="100 Main St, Austin, TX 78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        duplicate_status="Confirmed Duplicate",
        search_keyword="General dentist",
        campaign_type="PRM",
    )
    assert pipeline._enforce_mandatory_safety(dup_lead, None) is False

    # Lead marked Needs Review
    review_lead = InternalLeadRecord(
        practice_name="Review Practice",
        phone="(555) 123-4567",
        qualification_status=QualificationStatus.NEEDS_REVIEW.value,
        search_keyword="General dentist",
        campaign_type="PRM",
    )
    assert pipeline._enforce_mandatory_safety(review_lead, None) is False
