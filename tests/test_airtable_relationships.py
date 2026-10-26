"""Offline tests verifying Airtable relationship-aware ingestion and safety rules.

Covers all 14 required cases:
1. Qualified prospect creates only one Practice Pipeline record.
2. New prospect links to an existing approved ZIP record by record ID.
3. Unapproved or unmatched ZIP does not create a new ZIP record (routes to review).
4. DNC/referrer and duplicate records are excluded.
5. Contact records created only from genuine contact data (rejects placeholders/empty).
6. Outreach records created only from actual outreach events (canonical link, never secondary).
7. Appointment records created only from actual bookings.
8. Confirmations created only for real appointments and confirmation events.
9. Follow-ups created only by an authorized cadence rule or workflow.
10. QA records require an actual review event and valid outreach link.
11. Airtable inverse links and lookup fields are not manually duplicated or written to.
12. Retries do not create duplicate records (idempotency/dedupe checks).
13. Dry-run performs zero Airtable mutations.
14. Live ingestion remains disabled by default.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import requests
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config, ExecutionMode, QualificationStatus
from src.airtable_client import AirtableClient, AirtableError
from src.config_loader import ConfigLoader
from src.deduplicator import Deduplicator
from src.lead_normalizer import InternalLeadRecord, LeadNormalizer
from src.lead_qualifier import LeadQualifier
from src.pipeline import LeadPipeline


@pytest.fixture
def mock_airtable_client():
    mock_session = MagicMock()
    client = AirtableClient(
        api_key="pat_mock_valid_key",
        base_id="appnKumhO07VlzUe8",
        table_name="tblLzZp1Gc0Pvyvo5",
        session=mock_session,
    )
    # Mock cached approved ZIPs
    client._zip_records_cache = {"75001": "recZIP75001ABCDE", "78701": "recZIP78701FGHIJ"}
    return client, mock_session


# 1. Qualified prospect creates only one Practice Pipeline record.
def test_qualified_prospect_creates_only_one_practice_pipeline_record(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "records": [{"id": "recPractice12345", "fields": {"Practice Name": "Austin Family Dental"}}]
    }
    mock_session.request.return_value = mock_resp

    lead = InternalLeadRecord(
        practice_name="Austin Family Dental",
        phone="512-555-0199",
        address="100 Congress Ave, Austin, TX 78701",
        zip_code="78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        created = client.create_lead_records([lead], allow_ingestion=True)

    assert len(created) == 1
    assert mock_session.request.call_count == 1
    call_args = mock_session.request.call_args
    # Target endpoint must be Practice Pipeline (tblLzZp1Gc0Pvyvo5)
    assert "tblLzZp1Gc0Pvyvo5" in call_args[0][1]
    payload = call_args[1]["json"]
    assert len(payload["records"]) == 1
    # Only one record created in Practice Pipeline; no secondary tables called
    assert payload["records"][0]["fields"][AirtableClient.FIELD_PRACTICE_ZIP_LINK] == ["recZIP78701FGHIJ"]


# 2. New prospect links to existing approved ZIP record by record ID.
def test_new_prospect_links_to_existing_approved_zip_by_record_id(mock_airtable_client):
    client, _ = mock_airtable_client
    lead = InternalLeadRecord(
        practice_name="Dallas Dental Care",
        phone="214-555-0188",
        address="123 Main St, Dallas, TX 75001",
        zip_code="75001",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    fields = client.map_lead_to_airtable_fields(lead)
    # Must populate fldQTixIhZnJui3VE with the record ID array
    assert fields[AirtableClient.FIELD_PRACTICE_ZIP_LINK] == ["recZIP75001ABCDE"]
    # Must populate ZIP text only when supported
    assert fields[AirtableClient.FIELD_PRACTICE_ZIP_TEXT] == "75001"


# 3. Unapproved or unmatched ZIP does not cause a new ZIP record to be created.
def test_unapproved_or_unmatched_zip_does_not_create_zip_record(mock_airtable_client):
    client, mock_session = mock_airtable_client
    # Lead with unapproved ZIP code 99999
    lead = InternalLeadRecord(
        practice_name="Remote Dental",
        phone="555-999-0000",
        address="999 Nowhere Rd, TX 99999",
        zip_code="99999",
        qualification_status=QualificationStatus.NEEDS_REVIEW.value,
        qualification_reason="Territory 99999 not approved",
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        created = client.create_lead_records([lead], allow_ingestion=True)

    # Ingestion refused because lead is NEEDS_REVIEW
    assert created == []
    # No requests made to Zipcode List (tblu1CY9070zVsqot) or anywhere else
    assert mock_session.request.call_count == 0


# 4. DNC/referrer and duplicate records are excluded.
def test_dnc_and_duplicate_records_are_excluded(mock_airtable_client):
    client, mock_session = mock_airtable_client
    dnc_lead = InternalLeadRecord(
        practice_name="Blocked Referrer Dental",
        phone="512-555-0123",
        qualification_status=QualificationStatus.EXISTING_REFERRER_OR_DNC.value,
        qualification_reason="Matched DNC Phone",
        search_keyword="General Dentist",
        campaign_type="PRM",
    )
    duplicate_lead = InternalLeadRecord(
        practice_name="Duplicate Dental Practice",
        phone="512-555-0124",
        qualification_status=QualificationStatus.DUPLICATE.value,
        duplicate_status="Confirmed Duplicate",
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        created = client.create_lead_records([dnc_lead, duplicate_lead], allow_ingestion=True)

    assert created == []
    assert mock_session.request.call_count == 0


# 5. Contact records are created only from genuine contact data.
def test_contact_records_created_only_from_genuine_contact_data(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"records": [{"id": "recContact001", "fields": {}}]}
    mock_session.request.return_value = mock_resp

    # Valid genuine contact
    valid_contact = {
        "name": "Dr. Sarah Jenkins",
        "phone": "512-555-1122",
        "email": "dr.jenkins@austindental.com",
        "position": "Lead Dentist",
    }
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        res = client.create_contact_record("recPractice12345", valid_contact, allow_ingestion=True)
        assert res.get("id") == "recContact001"
        call_args = mock_session.request.call_args
        assert "tblU2E6ryS5eT8EVD" in call_args[0][1]
        fields = call_args[1]["json"]["records"][0]["fields"]
        assert fields[AirtableClient.FIELD_CONTACT_PRACTICE_LINK] == ["recPractice12345"]
        assert fields[AirtableClient.FIELD_CONTACT_NAME] == "Dr. Sarah Jenkins"

    # Fabricated / placeholder contact must be rejected
    placeholder_contact = {"name": "Doctor", "phone": "", "email": ""}
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Placeholder contact name"):
            client.create_contact_record("recPractice12345", placeholder_contact, allow_ingestion=True)

    # Empty contact must be rejected
    empty_contact = {"name": "", "phone": "", "email": ""}
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Genuine contact name, phone, or email is required"):
            client.create_contact_record("recPractice12345", empty_contact, allow_ingestion=True)


# 6. Outreach records are created only from actual outreach events.
def test_outreach_records_created_only_from_actual_outreach_events(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"records": [{"id": "recOutreach999", "fields": {}}]}
    mock_session.request.return_value = mock_resp

    actual_outreach = {
        "call_date": "2026-10-25",
        "call_outcome": "Spoke to DM",
        "call_notes": "Discussed PRM referral workflow, interested in follow-up call.",
        "spoke_to": "Dr. Sarah Jenkins",
        "recording_url": "https://recordings.example.com/call-123.mp3",
    }

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        res = client.create_outreach_record("recPractice12345", actual_outreach, allow_ingestion=True)
        assert res.get("id") == "recOutreach999"
        call_args = mock_session.request.call_args
        assert "tblxSfnQVSuJ3HgVj" in call_args[0][1]
        fields = call_args[1]["json"]["records"][0]["fields"]
        # Must link to canonical link fldNBDz2oEkaDNfJy
        assert fields[AirtableClient.FIELD_OUTREACH_PRACTICE_LINK] == ["recPractice12345"]
        # Must NEVER write to secondary link fldQo2uiLPDfL8heQ
        assert "fldQo2uiLPDfL8heQ" not in fields
        assert fields[AirtableClient.FIELD_OUTREACH_CALL_DATE] == "2026-10-25"
        assert fields[AirtableClient.FIELD_OUTREACH_CALL_OUTCOME] == "Spoke to DM"

    # Missing call outcome must be rejected
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Actual call date and call outcome are required"):
            client.create_outreach_record("recPractice12345", {"call_date": "2026-10-25"}, allow_ingestion=True)


# 7. Appointment records are created only from actual bookings.
def test_appointment_records_created_only_from_actual_bookings(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"records": [{"id": "recAppt111", "fields": {}}]}
    mock_session.request.return_value = mock_resp

    actual_appt = {
        "appt_date": "2026-10-30",
        "appt_time": "14:00",
        "setter": "Alex S.",
        "meeting_type": "Video Call",
        "notes": "PRM Introduction demo with Dr. Jenkins",
    }

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        res = client.create_sales_appt_record(
            "recPractice12345",
            actual_appt,
            zip_record_id="recZIP78701FGHIJ",
            allow_ingestion=True,
        )
        assert res.get("id") == "recAppt111"
        call_args = mock_session.request.call_args
        assert "tbl9Zc4mhG2AibN11" in call_args[0][1]
        fields = call_args[1]["json"]["records"][0]["fields"]
        assert fields[AirtableClient.FIELD_APPT_PRACTICE_LINK] == ["recPractice12345"]
        assert fields[AirtableClient.FIELD_APPT_ZIP_LINK] == ["recZIP78701FGHIJ"]
        assert fields[AirtableClient.FIELD_APPT_DATE] == "2026-10-30"

    # Missing appt date must be rejected
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Actual appointment date is required"):
            client.create_sales_appt_record("recPractice12345", {"setter": "Alex S."}, allow_ingestion=True)


# 8. Confirmations are created only for real appointments and confirmation events.
def test_confirmations_created_only_for_real_appointments(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"records": [{"id": "recConf222", "fields": {}}]}
    mock_session.request.return_value = mock_resp

    conf_data = {
        "confirmation_date": "2026-10-29",
        "status": "Confirmed",
        "confirmation_steps": "Spoke with OM Brenda, calendar invite accepted.",
    }

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        res = client.create_confirmation_record("recAppt111", conf_data, allow_ingestion=True)
        assert res.get("id") == "recConf222"
        call_args = mock_session.request.call_args
        assert "tblCzYikeEWixpKgi" in call_args[0][1]
        fields = call_args[1]["json"]["records"][0]["fields"]
        assert fields[AirtableClient.FIELD_CONFIRMATION_APPTS_LINK] == ["recAppt111"]
        assert fields[AirtableClient.FIELD_CONFIRMATION_STATUS] == "Confirmed"

    # Invalid appointment ID format must be rejected
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Valid Sales Appts record ID required"):
            client.create_confirmation_record("not-a-rec-id", conf_data, allow_ingestion=True)


# 9. Follow-ups are created only by an authorized cadence rule or workflow.
def test_follow_ups_created_only_by_authorized_cadence_rule(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"records": [{"id": "recFollow333", "fields": {}}]}
    mock_session.request.return_value = mock_resp

    follow_up_data = {
        "date": "2026-11-02",
        "cadence_step": "Day 3 Post-Appt Checkin",
        "status": "Pending",
        "om": "Brenda",
    }

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        res = client.create_follow_up_record(
            "recPractice12345",
            follow_up_data,
            sales_appt_record_id="recAppt111",
            allow_ingestion=True,
        )
        assert res.get("id") == "recFollow333"
        call_args = mock_session.request.call_args
        assert "tblkDeGSqlGWWUN82" in call_args[0][1]
        fields = call_args[1]["json"]["records"][0]["fields"]
        assert fields[AirtableClient.FIELD_FOLLOW_UP_PRACTICE_LINK] == ["recPractice12345"]
        assert fields[AirtableClient.FIELD_FOLLOW_UP_APPTS_LINK] == ["recAppt111"]
        assert fields[AirtableClient.FIELD_FOLLOW_UP_STEP] == "Day 3 Post-Appt Checkin"

    # Missing cadence step must be rejected
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Date and cadence step are required"):
            client.create_follow_up_record("recPractice12345", {"date": "2026-11-02"}, allow_ingestion=True)


# 10. QA records require an actual review event and valid outreach link.
def test_qa_records_require_actual_review_event_and_valid_outreach_link(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"records": [{"id": "recQA444", "fields": {}}]}
    mock_session.request.return_value = mock_resp

    qa_data = {
        "audit_date": "2026-10-26",
        "qa_by": "Senior Reviewer",
        "qa_checklist": "Passed introduction, verified objection handling",
        "feedback": "Great call opening and tone.",
    }

    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        res = client.create_qa_record("recOutreach999", qa_data, allow_ingestion=True)
        assert res.get("id") == "recQA444"
        call_args = mock_session.request.call_args
        assert "tblB7E8SGIUQbSsP4" in call_args[0][1]
        fields = call_args[1]["json"]["records"][0]["fields"]
        assert fields[AirtableClient.FIELD_QA_OUTREACH_LINK] == ["recOutreach999"]
        assert fields[AirtableClient.FIELD_QA_AUDIT_DATE] == "2026-10-26"

    # Missing audit date must be rejected
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        with pytest.raises(ValueError, match="Actual audit date is required"):
            client.create_qa_record("recOutreach999", {"qa_by": "Senior Reviewer"}, allow_ingestion=True)


# 11. Airtable inverse links and lookup fields are not manually duplicated or written to incorrectly.
def test_airtable_inverse_links_and_lookups_not_written(mock_airtable_client):
    client, _ = mock_airtable_client
    lead = InternalLeadRecord(
        practice_name="Modern Dental",
        phone="512-555-0100",
        address="100 Main St, Austin, TX 78701",
        zip_code="78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    fields = client.map_lead_to_airtable_fields(lead)
    # Check that reciprocal links and lookups are NOT in payload:
    # fldPXFMudmENIBAqQ: reciprocal link on Zipcode List (must never be written on Practice Pipeline)
    assert "fldPXFMudmENIBAqQ" not in fields
    # fldfmJkQJe6hVjQDZ: secondary outreach link (must not be populated)
    assert AirtableClient.FIELD_PRACTICE_OUTREACH_SECONDARY_LINK not in fields
    # Lookups from other tables:
    assert "fldGLpmHmczrgAEKI" not in fields
    assert "fldCbFgp0PrsXDG3i" not in fields
    assert "fldfEnfOf96aPNACd" not in fields


# 12. Retries do not create duplicate records.
def test_retries_do_not_create_duplicate_records():
    existing_records = [
        {
            "record_id": "recExisting001",
            "Practice Name": "Capitol Dental Care",
            "Phone Number": "512-555-0155",
            "Address": "500 Congress Ave, Austin, TX 78701",
            "Zipcode": "78701",
        }
    ]
    deduplicator = Deduplicator(existing_records=existing_records)

    incoming_lead = InternalLeadRecord(
        practice_name="Capitol Dental Care",
        phone="512-555-0155",
        address="500 Congress Ave, Austin, TX 78701",
        zip_code="78701",
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    batch = deduplicator.deduplicate_batch([incoming_lead])
    assert len(batch) == 1
    assert batch[0].duplicate_status == "Confirmed Duplicate"
    assert batch[0].qualification_status == QualificationStatus.DUPLICATE.value

    # If retried with create_lead_records, it must NOT be sent to Airtable
    mock_session = MagicMock()
    client = AirtableClient(api_key="pat_test", session=mock_session)
    with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
        created = client.create_lead_records(batch, allow_ingestion=True)
    assert created == []
    assert mock_session.request.call_count == 0


# 13. Dry-run performs no Airtable mutations.
def test_dry_run_performs_no_airtable_mutations(mock_airtable_client):
    client, mock_session = mock_airtable_client
    lead = InternalLeadRecord(
        practice_name="Safe Dry Run Clinic",
        phone="512-555-0199",
        zip_code="78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    # In dry-run mode (allow_ingestion=False), create_lead_records does 0 writes
    created_leads = client.create_lead_records([lead], allow_ingestion=False)
    assert created_leads == []

    # Contacts creation in dry run
    contact_res = client.create_contact_record("rec123", {"name": "Dr. Test", "phone": "555-0000"}, allow_ingestion=False)
    assert contact_res == {}

    # Outreach creation in dry run
    outreach_res = client.create_outreach_record("rec123", {"call_date": "2026-10-25", "call_outcome": "Left VM"}, allow_ingestion=False)
    assert outreach_res == {}

    # Sales appointment creation in dry run
    appt_res = client.create_sales_appt_record("rec123", {"appt_date": "2026-10-30"}, allow_ingestion=False)
    assert appt_res == {}

    # Confirmation creation in dry run
    conf_res = client.create_confirmation_record("recAppt123", {"confirmation_date": "2026-10-29", "status": "Confirmed"}, allow_ingestion=False)
    assert conf_res == {}

    # Follow up creation in dry run
    follow_res = client.create_follow_up_record("rec123", {"date": "2026-11-01", "cadence_step": "Call 1"}, allow_ingestion=False)
    assert follow_res == {}

    # QA creation in dry run
    qa_res = client.create_qa_record("recOutreach123", {"audit_date": "2026-10-26"}, allow_ingestion=False)
    assert qa_res == {}

    # Total HTTP calls must be 0
    assert mock_session.request.call_count == 0


# 14. Live ingestion remains disabled by default.
def test_live_ingestion_remains_disabled_by_default():
    assert Config.EXECUTION_MODE == ExecutionMode.DRY_RUN
    assert Config.ENABLE_LIVE_INGESTION is False
    assert Config.is_live_ingestion_authorized() is False


# 15. ZIP with approval flag blank -> not eligible for live ingestion.
def test_zip_with_approval_flag_blank_not_eligible(mock_airtable_client):
    client, mock_session = mock_airtable_client
    # Mock records from Zipcode List where 75002 has Approved Zipcodes=None/blank
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "records": [
            {"id": "recApproved1", "fields": {"Zip Code": "75001", "Approved Zipcodes": True}},
            {"id": "recBlank2", "fields": {"Zip Code": "75002", "Approved Zipcodes": None}},
            {"id": "recBlank3", "fields": {"Zip Code": "75003"}}, # field completely missing
        ]
    }
    mock_session.request.return_value = mock_resp

    approved_zips, approved_map = client.load_approved_zipcodes()
    assert "75001" in approved_zips
    assert "75002" not in approved_zips
    assert "75003" not in approved_zips
    assert "75002" not in approved_map
    assert "75003" not in approved_map

    # A lead with blank-flag ZIP 75002 must not receive a linked record ID
    lead = InternalLeadRecord(
        practice_name="Blank Zip Practice",
        phone="512-555-0102",
        zip_code="75002",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )
    fields = client.map_lead_to_airtable_fields(lead)
    assert AirtableClient.FIELD_PRACTICE_ZIP_LINK not in fields


# 16. ZIP explicitly unapproved -> not eligible for linking or live ingestion.
def test_zip_explicitly_unapproved_not_eligible(mock_airtable_client):
    client, mock_session = mock_airtable_client
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "records": [
            {"id": "recUnapproved1", "fields": {"Zip Code": "75004", "Approved Zipcodes": False}},
        ]
    }
    mock_session.request.return_value = mock_resp

    approved_zips, approved_map = client.load_approved_zipcodes()
    assert "75004" not in approved_zips
    assert "75004" not in approved_map

    lead = InternalLeadRecord(
        practice_name="Unapproved Zip Practice",
        phone="512-555-0104",
        zip_code="75004",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )
    fields = client.map_lead_to_airtable_fields(lead)
    assert AirtableClient.FIELD_PRACTICE_ZIP_LINK not in fields


# 17. Address/ZIP conflict -> review.
def test_address_zip_conflict_routes_to_review():
    raw_lead = {
        "title": "Conflicted Address Dental",
        "address": "100 Congress Ave, Austin, TX 78705",
        "postalCode": "78701", # Conflict: 78701 != 78705
        "phone": "+1 512-555-0199",
        "website": "https://conflicteddental.com",
    }
    lead = LeadNormalizer.normalize_lead(raw_lead, search_keyword="General dentist", campaign_type="PRM")
    assert any("Address/ZIP conflict" in issue for issue in lead.cleaning_issues)

    loader = ConfigLoader()
    bundle = loader.load_all()
    qualifier = LeadQualifier(bundle)
    qualifier.qualify(lead)

    assert lead.qualification_status == QualificationStatus.NEEDS_REVIEW.value
    assert "Address/ZIP conflict" in lead.qualification_reason


# 18. Ambiguous practice match -> review.
def test_ambiguous_practice_match_routes_to_review():
    existing_records = [
        {
            "record_id": "recKnown001",
            "Practice Name": "Austin Premier Dental",
            "Address": "100 Main St, Austin, TX 78701",
        }
    ]
    dedup = Deduplicator(existing_records=existing_records)

    # Lead with same name but completely different/unmatched address and no domain
    ambiguous_lead = InternalLeadRecord(
        practice_name="Austin Premier Dental",
        address="999 Completely Different Way, Houston, TX 77001",
        phone="713-555-9988",
        search_keyword="General dentist",
        campaign_type="PRM",
    )

    res = dedup.evaluate_lead(ambiguous_lead)
    assert res.duplicate_status == "Potential Duplicate"
    assert res.qualification_status == QualificationStatus.NEEDS_REVIEW.value
    assert "Ambiguous practice match" in res.qualification_reason


# 19. Existing practice with outreach history -> history preserved.
def test_existing_practice_outreach_history_preserved(mock_airtable_client):
    client, _ = mock_airtable_client
    # When mapping fields for prospect creation, outreach links are NOT overwritten
    lead = InternalLeadRecord(
        practice_name="Historic Dental Clinic",
        phone="512-555-0101",
        address="100 Congress Ave, Austin, TX 78701",
        zip_code="78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )
    fields = client.map_lead_to_airtable_fields(lead)
    # Outreach fields must never be present in prospect fields map
    assert AirtableClient.FIELD_PRACTICE_OUTREACH_PRIMARY_LINK not in fields
    assert AirtableClient.FIELD_PRACTICE_OUTREACH_SECONDARY_LINK not in fields


# 20. Airtable timeout or partial failure -> retry-safe and clearly reported.
def test_airtable_timeout_or_partial_failure_retry_safe():
    mock_session = MagicMock()
    mock_session.request.side_effect = requests.exceptions.Timeout("Connection timed out")

    client = AirtableClient(
        api_key="pat_test",
        base_id="appTest123",
        table_name="tblTest",
        session=mock_session,
    )

    lead = InternalLeadRecord(
        practice_name="Timeout Practice",
        phone="512-555-0101",
        zip_code="78701",
        qualification_status=QualificationStatus.QUALIFIED.value,
        search_keyword="General Dentist",
        campaign_type="PRM",
    )

    with patch("time.sleep", return_value=None):
        with patch.object(Config, "is_live_ingestion_authorized", return_value=True):
            with pytest.raises(AirtableError, match="Airtable batch insert failed|Connection timed out|failed"):
                client.create_lead_records([lead], allow_ingestion=True)
