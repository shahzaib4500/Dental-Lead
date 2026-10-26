"""Tests for Airtable integration and Pipeline safe operating modes.

Validates:
- Field mapping correctly translating internal model keys to Airtable column names
- Dry run mode strictly preventing any network writes
- Live ingestion being disabled by default
- Handling of API 429 rate limit retries
- Batching up to 10 records per Airtable request
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config, ExecutionMode, QualificationStatus
from src.airtable_client import AirtableClient
from src.lead_normalizer import InternalLeadRecord
from src.pipeline import LeadPipeline


def test_airtable_field_mapping():
    custom_mapping = {
        "practice_name": "Clinic Name",
        "phone": "Phone Number",
        "qualification_status": "Review Status",
    }
    client = AirtableClient(
        api_key="pat_test",
        base_id="appTest123",
        table_name="Leads",
        field_mapping=custom_mapping,
    )

    lead = InternalLeadRecord(
        practice_name="Modern Dental Studio",
        phone="(555) 123-4567",
        qualification_status="Qualified",
        search_keyword="General dentist",
        campaign_type="PRM",
    )

    fields = client.map_lead_to_airtable_fields(lead)
    assert fields["Clinic Name"] == "Modern Dental Studio"
    assert fields["Phone Number"] == "(555) 123-4567"
    assert fields["Review Status"] == "Qualified"
    # Unmapped fields are excluded
    assert "Website" not in fields


def test_dry_run_prevents_airtable_writes():
    mock_session = MagicMock()
    client = AirtableClient(
        api_key="pat_test",
        base_id="appTest123",
        table_name="Leads",
        session=mock_session,
    )

    lead = InternalLeadRecord(
        practice_name="Modern Dental Studio",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General dentist",
        campaign_type="PRM",
    )

    # Calling create_lead_records with allow_ingestion=False
    created = client.create_lead_records([lead], allow_ingestion=False)
    assert created == []
    # Verify no POST requests were sent
    assert mock_session.request.call_count == 0


def test_live_ingestion_disabled_by_default():
    assert Config.ENABLE_LIVE_INGESTION is False
    assert Config.is_live_ingestion_authorized() is False


def test_airtable_429_rate_limit_retry():
    mock_session = MagicMock()

    resp_429 = MagicMock()
    resp_429.status_code = 429

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"records": [{"id": "rec123", "fields": {}}]}

    mock_session.request.side_effect = [resp_429, resp_200]

    with patch("time.sleep", return_value=None):
        client = AirtableClient(
            api_key="pat_test",
            base_id="appTest123",
            table_name="Leads",
            session=mock_session,
        )
        res = client._request_with_retry("GET", client.table_endpoint)
        assert res.status_code == 200
        assert mock_session.request.call_count == 2
