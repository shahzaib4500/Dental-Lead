"""Tests for lead normalization module.

Validates:
- Name and whitespace cleaning
- Phone formatting (10-digit US format)
- Website sanitization and redirect stripping
- Address parsing (street, city, state, zip)
- Normalization of Apify Actor nwua9Gu5YrADL7ZDj schema fields:
  (title, categoryName, website, url, placeId, location: {lat, lng}, address, postalCode)
- Handling missing, null, or malformed fields without fabricating data
"""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.lead_normalizer import LeadNormalizer


def test_clean_name():
    assert LeadNormalizer.clean_name("  Smiling Dental   Group  ") == "Smiling Dental Group"
    assert LeadNormalizer.clean_name("") == ""
    assert LeadNormalizer.clean_name(None) == ""


def test_clean_phone():
    assert LeadNormalizer.clean_phone("1-800-555-0199") == "(800) 555-0199"
    assert LeadNormalizer.clean_phone("(555) 123-4567") == "(555) 123-4567"
    assert LeadNormalizer.clean_phone("5551234567") == "(555) 123-4567"
    assert LeadNormalizer.clean_phone("+1 555-123-4567") == "(555) 123-4567"
    assert LeadNormalizer.clean_phone("invalid") is None
    assert LeadNormalizer.clean_phone(None) is None


def test_clean_website():
    # Normal url
    assert LeadNormalizer.clean_website("https://exampledental.com") == "https://exampledental.com"
    # Auto-add https
    assert LeadNormalizer.clean_website("exampledental.com") == "https://exampledental.com"
    # Redirect url extraction
    redirect_url = "https://www.google.com/url?q=https%3A%2F%2Fdrsmithdental.com%2Fabout"
    assert LeadNormalizer.clean_website(redirect_url) == "https://drsmithdental.com/about"
    # Null cases
    assert LeadNormalizer.clean_website(None) is None
    assert LeadNormalizer.clean_website("null") is None


def test_parse_address_string():
    addr = "123 Main St, Beverly Hills, CA 90210"
    parsed = LeadNormalizer.parse_address_string(addr)
    assert parsed["street"] == "123 Main St"
    assert parsed["city"] == "Beverly Hills"
    assert parsed["state"] == "CA"
    assert parsed["zip_code"] == "90210"


def test_normalize_apify_actor_schema():
    """Verify exact mapping of Apify Actor nwua9Gu5YrADL7ZDj output dataset fields."""
    apify_item = {
        "title": "Austin Dental Center",
        "categoryName": "Dentist",
        "address": "711 W 38th St, Austin, TX 78705",
        "street": "711 W 38th St",
        "city": "Austin",
        "state": "TX",
        "postalCode": "78705",
        "phone": "+1 512-454-2744",
        "website": "https://austindentalcenter.com",
        "url": "https://www.google.com/maps/place/?q=place_id:ChIJPTacEpBQwokRKwIlDXelxkA",
        "placeId": "ChIJPTacEpBQwokRKwIlDXelxkA",
        "location": {
            "lat": 30.3015,
            "lng": -97.7428,
        },
    }

    lead = LeadNormalizer.normalize_lead(
        raw_data=apify_item,
        search_keyword="General dentist",
        campaign_type="PRM",
    )

    assert lead.practice_name == "Austin Dental Center"
    assert lead.category == "Dentist"
    assert lead.phone == "(512) 454-2744"
    assert lead.website == "https://austindentalcenter.com"
    assert lead.place_id == "ChIJPTacEpBQwokRKwIlDXelxkA"
    assert lead.google_maps_url == "https://www.google.com/maps/place/?q=place_id:ChIJPTacEpBQwokRKwIlDXelxkA"
    assert lead.city == "Austin"
    assert lead.state == "TX"
    assert lead.zip_code == "78705"
    assert lead.latitude == 30.3015
    assert lead.longitude == -97.7428
    assert lead.source == "Google Maps via Apify Actor"


def test_normalize_raw_lead_with_missing_fields():
    """Verify missing fields are safely handled without fabricating data."""
    raw_item = {
        "title": "Downtown Smiles",
        "categoryName": "Dental clinic",
    }
    lead = LeadNormalizer.normalize_lead(raw_item, search_keyword="General dentist", campaign_type="PRM")

    assert lead.practice_name == "Downtown Smiles"
    assert lead.phone is None
    assert lead.website is None
    assert lead.address is None
    assert lead.zip_code is None
    assert lead.category == "Dental clinic"
    assert lead.qualification_status == "Needs Review"
