"""Tests for lead qualification module.

Validates:
- PRM campaign targeting general dentistry
- Wisdom Sales campaign targeting specialists
- Exclusion of ClearChoice / Affordable Dentures
- Detection and flagging of DSO affiliations
- Exclusions for Existing Referrers and DNC lists
- Territory restrictions (approved vs outside ZIPs)
- Unconfigured territory handling (marked Needs Review)
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import CampaignType, QualificationStatus
from src.config_loader import ConfigLoader, EntityEntry
from src.lead_normalizer import InternalLeadRecord
from src.lead_qualifier import LeadQualifier


def test_existing_referrer_and_dnc_exclusion():
    loader = ConfigLoader()
    bundle = loader.load_all()
    bundle.territories.approved_zip_codes = ["90210"]
    bundle.existing_referrers.entries = [
        EntityEntry(type="name", value="Dr. Alice Smith Dental", active=True),
        EntityEntry(type="phone", value="5551112222", active=True),
    ]
    bundle.do_not_contact.entries = [
        EntityEntry(type="name", value="Do Not Contact Clinic", active=True),
        EntityEntry(type="domain", value="dnc-dental.com", active=True),
    ]

    qualifier = LeadQualifier(bundle)

    # Matched Referrer
    lead1 = InternalLeadRecord(
        practice_name="Dr. Alice Smith Dental",
        phone="(555) 111-2222",
        website="https://alicesmithdds.com",
        address="123 Beverly Blvd",
        zip_code="90210",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res1 = qualifier.qualify(lead1)
    assert res1.qualification_status == QualificationStatus.EXISTING_REFERRER_OR_DNC.value
    assert "Existing Referrer" in res1.qualification_reason

    # Matched DNC
    lead2 = InternalLeadRecord(
        practice_name="Some Practice",
        phone="(555) 333-1111",
        website="https://dnc-dental.com",
        address="123 Beverly Blvd",
        zip_code="90210",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res2 = qualifier.qualify(lead2)
    assert res2.qualification_status == QualificationStatus.EXISTING_REFERRER_OR_DNC.value
    assert "Do-Not-Contact" in res2.qualification_reason


def test_territory_validation():
    loader = ConfigLoader()
    bundle = loader.load_all()
    bundle.territories.approved_zip_codes = ["90210", "90211"]

    qualifier = LeadQualifier(bundle)

    # Outside approved territory
    lead = InternalLeadRecord(
        practice_name="Outer Valley Dental",
        phone="(555) 999-0000",
        website="https://outervalleydental.com",
        address="100 Valley Rd",
        zip_code="10001",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res = qualifier.qualify(lead)
    assert res.qualification_status == QualificationStatus.DISQUALIFIED.value
    assert "outside approved" in res.qualification_reason.lower()


def test_prm_qualification_and_dso_flag():
    loader = ConfigLoader()
    bundle = loader.load_all()
    bundle.territories.approved_zip_codes = ["90210"]
    bundle.dso_keywords.keywords = ["Heartland Dental", "Aspen Dental"]

    qualifier = LeadQualifier(bundle)

    # Qualified independent general dentist
    lead_indep = InternalLeadRecord(
        practice_name="Beverly Hills Family Dentistry",
        category="General dentist",
        phone="(555) 234-5678",
        website="https://bhfamilydentistry.com",
        address="9000 Wilshire Blvd",
        zip_code="90210",
        search_keyword="Family dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res_indep = qualifier.qualify(lead_indep)
    assert res_indep.qualification_status == QualificationStatus.QUALIFIED.value

    # Confirmed DSO flag marked Needs Review
    lead_dso = InternalLeadRecord(
        practice_name="Aspen Dental Associates",
        category="Dentist",
        phone="(555) 456-7890",
        website="https://aspendental.com",
        address="9100 Wilshire Blvd",
        zip_code="90210",
        search_keyword="General dentist",
        campaign_type=CampaignType.PRM.value,
    )
    res_dso = qualifier.qualify(lead_dso)
    assert res_dso.qualification_status == QualificationStatus.NEEDS_REVIEW.value
    assert "DSO keyword match" in res_dso.qualification_reason


def test_wisdom_sales_specialists_and_chain_exclusions():
    loader = ConfigLoader()
    bundle = loader.load_all()
    bundle.territories.approved_zip_codes = ["90210"]
    bundle.excluded_chains.wisdom_sales = ["ClearChoice", "Affordable Dentures"]

    qualifier = LeadQualifier(bundle)

    # Excluded national chain
    lead_clearchoice = InternalLeadRecord(
        practice_name="ClearChoice Dental Implant Center",
        phone="(555) 777-8888",
        website="https://clearchoice.com",
        address="9200 Wilshire Blvd",
        zip_code="90210",
        search_keyword="Oral surgeon",
        campaign_type=CampaignType.WISDOM_SALES.value,
    )
    res_cc = qualifier.qualify(lead_clearchoice)
    assert res_cc.qualification_status == QualificationStatus.DISQUALIFIED.value
    assert "exclusion: National chain" in res_cc.qualification_reason

    # Qualified periodontist specialist
    lead_perio = InternalLeadRecord(
        practice_name="Rodeo Drive Periodontics",
        category="Periodontist",
        phone="(555) 888-9999",
        website="https://rodeodriveperiodontics.com",
        address="9300 Wilshire Blvd",
        zip_code="90210",
        search_keyword="Periodontist",
        campaign_type=CampaignType.WISDOM_SALES.value,
    )
    res_perio = qualifier.qualify(lead_perio)
    assert res_perio.qualification_status == QualificationStatus.QUALIFIED.value
