"""Apify Google Maps Actor client (Actor: nwua9Gu5YrADL7ZDj - compass/crawler-google-places).

Executes Google Maps searches through Apify API v2 without using Google Places API.
Supports starting runs, polling execution lifecycle (READY, RUNNING, SUCCEEDED, FAILED, TIMED-OUT),
pagination, and reading dataset items. Never logs credentials.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional
import requests

from config import Config

logger = logging.getLogger(__name__)


class ApifyError(Exception):
    """Exception raised on Apify API or Actor execution failure."""
    pass


class ApifyGoogleMapsClient:
    """Client for running and retrieving results from the Apify Google Maps Actor."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        actor_id: Optional[str] = None,
        api_base: Optional[str] = None,
        run_timeout: Optional[int] = None,
        session: Optional[requests.Session] = None,
    ):
        self.api_token = api_token or Config.APIFY_API_TOKEN
        self.actor_id = actor_id or Config.APIFY_ACTOR_ID
        self.api_base = (api_base or Config.APIFY_API_BASE).rstrip("/")
        self.run_timeout = run_timeout or Config.APIFY_RUN_TIMEOUT
        self.session = session or requests.Session()

    @property
    def headers(self) -> Dict[str, str]:
        """Request headers with Bearer token authentication."""
        if not self.api_token:
            raise ApifyError("APIFY_API_TOKEN is missing. Please set it in your environment or .env file.")
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def build_actor_input(
        self,
        search_terms: List[str],
        location: Optional[str] = None,
        max_crawled_places: Optional[int] = None,
        language: Optional[str] = None,
        skip_closed_places: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Construct input payload matching the confirmed schema for nwua9Gu5YrADL7ZDj."""
        payload: Dict[str, Any] = {
            "searchStringsArray": search_terms if isinstance(search_terms, list) else [search_terms],
            "language": language or Config.APIFY_LANGUAGE,
            "maxCrawledPlacesPerSearch": max_crawled_places or Config.APIFY_MAX_CRAWLED_PLACES_PER_SEARCH,
        }

        if location and location.strip():
            payload["locationQuery"] = location.strip()

        if skip_closed_places is not None:
            payload["skipClosedPlaces"] = skip_closed_places
        else:
            payload["skipClosedPlaces"] = Config.APIFY_SKIP_CLOSED_PLACES

        # Keep extra paid add-ons strictly disabled to avoid unnecessary charges
        payload["maxReviews"] = 0
        payload["maxImages"] = 0
        payload["scrapeContacts"] = False

        return payload

    def search(
        self,
        keyword: str,
        location: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Convenience method for searching a single dental keyword."""
        return self.run_search_and_fetch(
            search_terms=[keyword],
            location=location,
            max_results_per_search=max_results,
        )

    def run_search_and_fetch(
        self,
        search_terms: List[str],
        location: Optional[str] = None,
        max_results_per_search: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Execute Actor run, poll until completion, and retrieve dataset items.

        Handles:
        - Input construction
        - Actor run dispatch
        - Status polling (SUCCEEDED, RUNNING, FAILED, TIMED-OUT)
        - Dataset retrieval with pagination
        - Network error handling and rate limits
        """
        if not self.api_token:
            raise ApifyError("Apify API token is not configured.")

        actor_input = self.build_actor_input(
            search_terms=search_terms,
            location=location,
            max_crawled_places=max_results_per_search,
        )

        # 1. Start the Actor run
        run_endpoint = f"{self.api_base}/acts/{self.actor_id}/runs"
        logger.info(f"Starting Apify Actor '{self.actor_id}' run with search terms: {search_terms}, location: {location}")

        try:
            res = self.session.post(
                run_endpoint,
                headers=self.headers,
                json=actor_input,
                timeout=30,
            )
        except requests.RequestException as e:
            logger.error(f"Failed to connect to Apify API: {e}")
            raise ApifyError(f"Network error starting Apify Actor: {e}") from e

        if res.status_code == 401 or res.status_code == 403:
            raise ApifyError("Apify authentication failed (HTTP 401/403). Check your APIFY_API_TOKEN.")
        elif res.status_code == 429:
            raise ApifyError("Apify API rate limit exceeded (HTTP 429).")
        elif res.status_code not in (200, 201):
            raise ApifyError(f"Failed to start Apify Actor ({res.status_code}): {res.text}")

        run_data = res.json().get("data", {})
        run_id = run_data.get("id")
        dataset_id = run_data.get("defaultDatasetId")

        if not run_id:
            raise ApifyError(f"Invalid response from Apify: missing run ID ({res.text})")

        logger.info(f"Apify run started successfully. Run ID: {run_id}, Default Dataset ID: {dataset_id}")

        # 2. Wait for Actor run completion
        status = self._poll_run_status(run_id)
        if status != "SUCCEEDED":
            raise ApifyError(f"Apify Actor run {run_id} finished with non-successful status: {status}")

        # 3. Fetch items from default dataset
        return self.fetch_dataset_items(dataset_id or run_data.get("defaultDatasetId"))

    def _poll_run_status(self, run_id: str, poll_interval: int = 5) -> str:
        """Poll the run status until terminal state or timeout."""
        status_endpoint = f"{self.api_base}/actor-runs/{run_id}"
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > self.run_timeout:
                logger.error(f"Apify Actor run {run_id} timed out after {self.run_timeout} seconds.")
                raise ApifyError(f"Timeout waiting for Apify Actor run {run_id} to complete.")

            try:
                res = self.session.get(status_endpoint, headers=self.headers, timeout=15)
            except requests.RequestException as e:
                logger.warning(f"Transient error polling run status for {run_id}: {e}")
                time.sleep(poll_interval)
                continue

            if res.status_code == 200:
                run_info = res.json().get("data", {})
                status = run_info.get("status")
                logger.info(f"Apify run {run_id} status: {status} (elapsed: {int(elapsed)}s)")

                if status in ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"):
                    return status
            elif res.status_code == 429:
                logger.warning("Apify rate limit hit while polling. Waiting 10s...")
                time.sleep(10)
                continue
            else:
                logger.error(f"Error checking run status ({res.status_code}): {res.text}")

            time.sleep(poll_interval)

    def fetch_dataset_items(self, dataset_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve all items from an Apify dataset with pagination support."""
        if not dataset_id:
            logger.warning("No dataset ID provided to fetch items from.")
            return []

        dataset_endpoint = f"{self.api_base}/datasets/{dataset_id}/items"
        all_items: List[Dict[str, Any]] = []
        offset = 0

        while True:
            params = {
                "format": "json",
                "clean": "true",
                "limit": limit,
                "offset": offset,
            }

            try:
                res = self.session.get(dataset_endpoint, headers=self.headers, params=params, timeout=30)
            except requests.RequestException as e:
                logger.error(f"Failed to fetch dataset items for dataset {dataset_id}: {e}")
                raise ApifyError(f"Network error fetching Apify dataset items: {e}") from e

            if res.status_code != 200:
                raise ApifyError(f"Failed to fetch dataset items ({res.status_code}): {res.text}")

            items = res.json()
            if not isinstance(items, list):
                logger.warning(f"Unexpected dataset response format: {type(items)}")
                break

            all_items.extend(items)
            if len(items) < limit:
                break

            offset += len(items)

        logger.info(f"Retrieved {len(all_items)} raw place items from Apify dataset {dataset_id}")
        return all_items
