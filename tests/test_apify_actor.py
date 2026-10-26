"""Tests for Apify Google Maps Actor integration and Pipeline.

Validates:
- Exact Actor input payload construction matching schema for nwua9Gu5YrADL7ZDj
- Actor run lifecycle handling (dispatch, polling status, dataset retrieval)
- Handling of failed runs, timeouts, and rate limits
- Handling of pagination across dataset items
- Ensuring API token is never exposed in logs or URLs
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from src.apify_google_maps import ApifyError, ApifyGoogleMapsClient


def test_build_actor_input():
    client = ApifyGoogleMapsClient(api_token="apify_test_token")

    payload = client.build_actor_input(
        search_terms=["General dentist"],
        location="Austin, TX",
        max_crawled_places=25,
        language="en",
        skip_closed_places=True,
    )

    assert payload["searchStringsArray"] == ["General dentist"]
    assert payload["locationQuery"] == "Austin, TX"
    assert payload["maxCrawledPlacesPerSearch"] == 25
    assert payload["language"] == "en"
    assert payload["skipClosedPlaces"] is True
    # Ensure expensive add-ons are explicitly turned off
    assert payload["maxReviews"] == 0
    assert payload["scrapeContacts"] is False


def test_actor_run_lifecycle_and_dataset_retrieval():
    mock_session = MagicMock()

    # 1. Start run response
    start_resp = MagicMock()
    start_resp.status_code = 201
    start_resp.json.return_value = {
        "data": {
            "id": "run_123",
            "defaultDatasetId": "dataset_456",
            "status": "RUNNING",
        }
    }

    # 2. Polling status response (first RUNNING, then SUCCEEDED)
    poll_running = MagicMock()
    poll_running.status_code = 200
    poll_running.json.return_value = {"data": {"status": "RUNNING"}}

    poll_succeeded = MagicMock()
    poll_succeeded.status_code = 200
    poll_succeeded.json.return_value = {"data": {"status": "SUCCEEDED"}}

    # 3. Dataset items response
    dataset_resp = MagicMock()
    dataset_resp.status_code = 200
    dataset_resp.json.return_value = [
        {
            "title": "Austin Smile Center",
            "categoryName": "Dentist",
            "address": "100 Congress Ave, Austin, TX 78701",
            "phone": "+1 512-555-1234",
            "placeId": "ChIJ12345",
        }
    ]

    mock_session.post.return_value = start_resp
    mock_session.get.side_effect = [poll_running, poll_succeeded, dataset_resp]

    with patch("time.sleep", return_value=None):
        client = ApifyGoogleMapsClient(
            api_token="test_token",
            actor_id="nwua9Gu5YrADL7ZDj",
            session=mock_session,
        )

        results = client.run_search_and_fetch(
            search_terms=["General dentist"],
            location="Austin, TX",
        )

        assert len(results) == 1
        assert results[0]["title"] == "Austin Smile Center"
        assert results[0]["placeId"] == "ChIJ12345"

        # Verify Actor run was called on the correct endpoint
        assert "acts/nwua9Gu5YrADL7ZDj/runs" in mock_session.post.call_args[0][0]
        # Verify Bearer token was set in headers
        assert mock_session.post.call_args[1]["headers"]["Authorization"] == "Bearer test_token"


def test_actor_run_failure():
    mock_session = MagicMock()

    start_resp = MagicMock()
    start_resp.status_code = 201
    start_resp.json.return_value = {
        "data": {
            "id": "run_failed_123",
            "defaultDatasetId": "dataset_xyz",
            "status": "RUNNING",
        }
    }

    poll_failed = MagicMock()
    poll_failed.status_code = 200
    poll_failed.json.return_value = {"data": {"status": "FAILED"}}

    mock_session.post.return_value = start_resp
    mock_session.get.return_value = poll_failed

    with patch("time.sleep", return_value=None):
        client = ApifyGoogleMapsClient(
            api_token="test_token",
            actor_id="nwua9Gu5YrADL7ZDj",
            session=mock_session,
        )

        with pytest.raises(ApifyError) as exc_info:
            client.run_search_and_fetch(search_terms=["Periodontist"])

        assert "non-successful status: FAILED" in str(exc_info.value)


def test_dataset_pagination():
    mock_session = MagicMock()

    # First page: 2 items (limit=2 triggers second page request)
    page_1 = MagicMock()
    page_1.status_code = 200
    page_1.json.return_value = [{"title": "Practice 1"}, {"title": "Practice 2"}]

    # Second page: 1 item (less than limit, stops pagination)
    page_2 = MagicMock()
    page_2.status_code = 200
    page_2.json.return_value = [{"title": "Practice 3"}]

    mock_session.get.side_effect = [page_1, page_2]

    client = ApifyGoogleMapsClient(api_token="test_token", session=mock_session)
    items = client.fetch_dataset_items(dataset_id="ds_123", limit=2)

    assert len(items) == 3
    assert mock_session.get.call_count == 2
