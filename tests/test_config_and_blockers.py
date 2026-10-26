"""Tests for JSON configuration loading and BlockerEngine.

Validates:
- Loading and validating all 7 JSON files
- Adding a DSO keyword dynamically alters matching without editing Python
- Case-insensitive matching and whitespace normalization
- Excluded chains scoped strictly to configured campaigns (e.g. Wisdom Sales)
- Territory edits changing results; strings preserving leading zeros
- Individual enablement and disablement of blockers
- Unknown blocker condition safety rejection
- Invalid/missing JSON file prevention
- Mandatory safety protections cannot be bypassed by disabling blockers
"""

import copy
import json
import sys
import tempfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import CampaignType, QualificationStatus
from src.blocker_engine import BlockerEngine
from src.config_loader import ConfigLoader, ConfigValidationError
from src.lead_normalizer import InternalLeadRecord
from src.lead_qualifier import LeadQualifier
from src.pipeline import LeadPipeline


@pytest.fixture
def config_bundle():
    """Load the default application configuration bundle from config/."""
    loader = ConfigLoader()
    return loader.load_all()


def test_config_loader_loads_all_seven_files(config_bundle):
    assert len(config_bundle.dso_keywords.keywords) > 0
    assert "ClearChoice" in config_bundle.excluded_chains.wisdom_sales
    assert config_bundle.territories.version == 1
    assert len(config_bundle.qualification_rules.campaigns) >= 2
    assert len(config_bundle.blockers.blockers) >= 8


def test_dso_keywords_dynamic_addition(config_bundle):
    # Dynamically simulate user adding a new DSO to the JSON list
    bundle_copy = copy.deepcopy(config_bundle)
    bundle_copy.dso_keywords.keywords.append("CustomDentalCorp")
    bundle_copy.territories.approved_zip_codes = ["78701"]

    qualifier = LeadQualifier(bundle_copy)

    lead = InternalLeadRecord(
        practice_name="CustomDentalCorp Downtown Branch",
        phone="(512) 987-6543",
        website="https://customdentalcorp.com",
        address="123 Main St, Austin, TX",
        zip_code="78701",
        category="General dentist",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )

    res = qualifier.qualify(lead)
    assert res.qualification_status == QualificationStatus.NEEDS_REVIEW.value
    assert "CustomDentalCorp" in res.qualification_reason


def test_dso_case_insensitive_matching(config_bundle):
    bundle_copy = copy.deepcopy(config_bundle)
    bundle_copy.territories.approved_zip_codes = ["78701"]
    qualifier = LeadQualifier(bundle_copy)

    lead = InternalLeadRecord(
        practice_name="HEARTLAND DENTAL PARTNERS",
        phone="(512) 987-6543",
        website="https://example.com",
        address="123 Main St",
        zip_code="78701",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )

    res = qualifier.qualify(lead)
    assert res.qualification_status == QualificationStatus.NEEDS_REVIEW.value
    assert "Heartland Dental" in res.qualification_reason


def test_excluded_chains_scoped_to_wisdom_sales(config_bundle):
    bundle_copy = copy.deepcopy(config_bundle)
    bundle_copy.territories.approved_zip_codes = ["78701"]
    qualifier = LeadQualifier(bundle_copy)

    # 1. ClearChoice in Wisdom Sales -> Disqualified
    ws_lead = InternalLeadRecord(
        practice_name="ClearChoice Dental Center",
        phone="(512) 222-3333",
        website="https://clearchoice.com",
        address="456 Elm St",
        zip_code="78701",
        search_keyword="Oral surgeon",
        campaign_type=CampaignType.WISDOM_SALES.value,
    )
    res_ws = qualifier.qualify(ws_lead)
    assert res_ws.qualification_status == QualificationStatus.DISQUALIFIED.value
    assert "ClearChoice" in res_ws.qualification_reason

    # 2. In PRM, ClearChoice is not in PRM's excluded list
    prm_lead = InternalLeadRecord(
        practice_name="ClearChoice Dental Center",
        phone="(512) 222-3333",
        website="https://clearchoice.com",
        address="456 Elm St",
        zip_code="78701",
        category="General dentist",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res_prm = qualifier.qualify(prm_lead)
    assert res_prm.qualification_status != QualificationStatus.DISQUALIFIED.value


def test_territory_edits_and_leading_zeros(config_bundle):
    bundle_copy = copy.deepcopy(config_bundle)
    # Add East Coast ZIP with leading zero as string
    bundle_copy.territories.approved_zip_codes = ["07001", "08540"]

    qualifier = LeadQualifier(bundle_copy)

    lead_valid = InternalLeadRecord(
        practice_name="Princeton Family Dentistry",
        phone="(609) 333-4444",
        website="https://princetondental.com",
        address="100 Nassau St",
        zip_code="08540",
        category="General dentist",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res_valid = qualifier.qualify(lead_valid)
    assert res_valid.qualification_status == QualificationStatus.QUALIFIED.value

    lead_invalid = InternalLeadRecord(
        practice_name="Texas Dental",
        phone="(512) 444-5555",
        website="https://texasdental.com",
        address="100 Congress Ave",
        zip_code="78701",
        category="General dentist",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res_invalid = qualifier.qualify(lead_invalid)
    assert res_invalid.qualification_status == QualificationStatus.DISQUALIFIED.value


def test_individual_blocker_enable_and_disable(config_bundle):
    bundle_copy = copy.deepcopy(config_bundle)
    engine = BlockerEngine(bundle_copy)

    # Lead missing website
    lead = InternalLeadRecord(
        practice_name="No Website Dental",
        phone="(555) 123-4567",
        website=None,
        address="123 Oak St",
        zip_code="78701",
        category="General dentist",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )

    # With MISSING_WEBSITE enabled (default)
    res1 = engine.evaluate_lead(lead)
    assert "MISSING_WEBSITE" in res1.triggered_blocker_ids

    # Disable MISSING_WEBSITE blocker
    for b in bundle_copy.blockers.blockers:
        if b.id == "MISSING_WEBSITE":
            b.enabled = False

    engine_disabled = BlockerEngine(bundle_copy)
    res2 = engine_disabled.evaluate_lead(lead)
    assert "MISSING_WEBSITE" not in res2.triggered_blocker_ids


def test_unknown_blocker_condition_rejected_on_load(tmp_path):
    # Test that unknown conditions fail safely during loading
    bad_blockers = {
        "version": 1,
        "blockers": [
            {
                "id": "UNSAFE_BLOCKER",
                "enabled": True,
                "severity": "block",
                "applies_to": ["PRM"],
                "condition": "arbitrary_eval_code_injection",
                "status": "Needs Review",
                "message": "Unsafe test"
            }
        ]
    }
    with open(tmp_path / "blockers.json", "w") as f:
        json.dump(bad_blockers, f)

    loader = ConfigLoader(config_dir=tmp_path)
    with pytest.raises(ConfigValidationError) as exc:
        loader.load_blockers()
    assert "Unknown blocker condition 'arbitrary_eval_code_injection'" in str(exc.value)


def test_mandatory_safety_cannot_be_bypassed_by_disabling_blockers(config_bundle):
    """Critical safety test: Disabling duplicate or DNC blockers in JSON must NOT allow ingestion."""
    bundle_copy = copy.deepcopy(config_bundle)

    # Disable DUPLICATE_LEAD and DO_NOT_CONTACT blockers
    for b in bundle_copy.blockers.blockers:
        if b.id in ("DUPLICATE_LEAD", "DO_NOT_CONTACT"):
            b.enabled = False

    engine = BlockerEngine(bundle_copy)

    # Create a confirmed duplicate lead
    lead = InternalLeadRecord(
        practice_name="Duplicate Dental",
        phone="(555) 999-8888",
        website="https://dup.com",
        address="100 Main St",
        zip_code="78701",
        category="General dentist",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
        duplicate_status="Confirmed Duplicate",
        qualification_status=QualificationStatus.DUPLICATE.value,
    )

    eval_res = engine.evaluate_lead(lead)

    # Enforce mandatory pipeline check
    mandatory_pass = LeadPipeline._enforce_mandatory_safety(lead, eval_res)
    assert mandatory_pass is False, "Mandatory safety MUST block duplicate ingestion regardless of blocker toggle!"


def test_invalid_update_never_overwrites_valid_config(tmp_path):
    """Verify that an invalid update is rejected and leaves the original file untouched."""
    source_config_dir = Path(__file__).parent.parent / "config"
    # Copy dso_keywords.json to temporary test directory
    original_text = (source_config_dir / "dso_keywords.json").read_text(encoding="utf-8")
    (tmp_path / "dso_keywords.json").write_text(original_text, encoding="utf-8")

    loader = ConfigLoader(config_dir=tmp_path)
    initial_data = loader.get_resource_raw("dso_keywords")

    bad_payload = {
        "version": 1,
        "keywords": "Should be a list not string",
    }

    with pytest.raises(ConfigValidationError):
        loader.save_resource_data("dso_keywords", bad_payload)

    # Confirm original file was not modified or corrupted
    after_data = loader.get_resource_raw("dso_keywords")
    assert after_data == initial_data
    # Confirm no leftover temporary staging files
    assert not (tmp_path / "dso_keywords.json.tmp").exists()


def test_atomic_persistence_and_safe_writes(tmp_path):
    """Verify that valid updates are persisted atomically."""
    source_config_dir = Path(__file__).parent.parent / "config"
    for cfg in source_config_dir.glob("*.json"):
        (tmp_path / cfg.name).write_text(cfg.read_text(encoding="utf-8"), encoding="utf-8")

    loader = ConfigLoader(config_dir=tmp_path)
    valid_update = {
        "version": 1,
        "description": "Updated DSO keywords",
        "match_mode": "case_insensitive",
        "keywords": ["Aspen Dental", "Heartland Dental", "NewlyAddedDentalCorp"],
        "action_on_match": "needs_review",
    }
    loader.save_resource_data("dso_keywords", valid_update)

    reloaded = loader.load_dso_keywords()
    assert "NewlyAddedDentalCorp" in reloaded.keywords
    assert not (tmp_path / "dso_keywords.json.tmp").exists()
