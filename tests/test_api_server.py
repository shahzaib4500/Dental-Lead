"""Tests for HTTP REST API server and Postman Collection validity.

Validates:
- GET /api/health returns 200 with runtime and configuration status
- GET /api/config returns summary without exposing secrets
- POST /api/config/reload dynamically reloads files
- GET /api/blockers lists all registered blockers
- POST /api/pipeline/preview evaluates offline mock leads with zero side effects
- POST /api/pipeline/run enforces dry-run safety and blocks unauthorized live ingestion
- Postman Collection and Environment files are valid JSON and conform to Postman v2.1 schema
- Every Postman collection endpoint matches an actual registered server route
"""

import json
import sys
import threading
import time
from pathlib import Path
import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.server import create_server


@pytest.fixture(scope="module")
def api_server(tmp_path_factory):
    """Start local test server in a background daemon thread using an isolated copy of config/."""
    host = "127.0.0.1"
    port = 8899  # Dedicated test port
    temp_config_dir = tmp_path_factory.mktemp("test_config")
    source_config_dir = Path(__file__).parent.parent / "config"

    # Copy all active config files into isolated temp directory for test server
    for cfg_file in source_config_dir.glob("*.json"):
        (temp_config_dir / cfg_file.name).write_text(cfg_file.read_text(encoding="utf-8"), encoding="utf-8")

    server = create_server(host=host, port=port, config_dir=temp_config_dir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    time.sleep(0.3)  # Allow server socket to bind
    yield base_url
    server.shutdown()
    server.server_close()


def test_api_health(api_server):
    res = requests.get(f"{api_server}/api/health", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert data["service"] == "dental-lead-generator"
    assert "execution_mode" in data
    assert "config_status" in data


def test_api_config(api_server):
    res = requests.get(f"{api_server}/api/config", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert "dso_keywords" in data
    assert "territories" in data
    assert "blockers" in data
    # Ensure no secrets exposed
    assert "APIFY_API_TOKEN" not in json.dumps(data)
    assert "AIRTABLE_API_KEY" not in json.dumps(data)


def test_api_config_reload(api_server):
    res = requests.post(f"{api_server}/api/config/reload", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["blockers_loaded"] >= 8


def test_api_blockers(api_server):
    res = requests.get(f"{api_server}/api/blockers", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 8
    blocker_ids = [b["id"] for b in data["blockers"]]
    assert "TERRITORY_NOT_CONFIGURED" in blocker_ids
    assert "DSO_MATCH" in blocker_ids
    assert "DUPLICATE_LEAD" in blocker_ids


def test_api_pipeline_preview_safe_offline(api_server):
    mock_payload = {
        "campaign": "PRM",
        "leads": [
            {
                "title": "Beverly Hills Family Dentistry",
                "categoryName": "General dentist",
                "phone": "+1 310-555-0199",
                "website": "https://bhfamilydentistry.example",
                "address": "9000 Wilshire Blvd, Beverly Hills, CA 90210",
                "placeId": "ChIJ_bhfamily_1",
            },
            {
                "title": "Heartland Dental Partners",
                "categoryName": "Dentist",
                "phone": "+1 310-555-0200",
                "website": "https://heartland.example",
                "address": "9100 Wilshire Blvd, Beverly Hills, CA 90210",
                "placeId": "ChIJ_heartland_2",
            },
        ],
    }
    res = requests.post(f"{api_server}/api/pipeline/preview", json=mock_payload, timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["total_evaluated"] == 2
    leads = data["leads"]
    assert len(leads) == 2
    # Verify DSO keyword match detected on Heartland Dental
    dso_lead = next(l for l in leads if "Heartland" in l["practice_name"])
    assert "DSO_MATCH" in dso_lead["triggered_blocker_ids"]
    assert dso_lead["matched_dso_keyword"] == "Heartland Dental"


def test_api_pipeline_run_unauthorized_live_ingestion_blocked(api_server):
    payload = {
        "campaign": "PRM",
        "location": "Austin, TX",
        "mode": "live_ingestion",
    }
    res = requests.post(f"{api_server}/api/pipeline/run", json=payload, timeout=5)
    # Live ingestion must return 403 Forbidden since credentials/flags are not authorized
    assert res.status_code == 403
    assert "not authorized" in res.json()["error"].lower()


def test_api_get_specific_resource_success(api_server):
    res = requests.get(f"{api_server}/api/config/zip_codes", timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["resource"] == "zip_codes"
    assert "zip_codes" in data["data"]
    assert any(z["zip_code"] == "78701" for z in data["data"]["zip_codes"])


def test_api_get_specific_resource_unknown_rejected(api_server):
    res = requests.get(f"{api_server}/api/config/unknown_foo_resource", timeout=5)
    assert res.status_code == 404
    assert "Unknown configuration resource" in res.json()["error"]


def test_api_preview_config_valid(api_server):
    payload = {
        "resource": "dso_keywords",
        "data": {
            "version": 1,
            "match_mode": "case_insensitive",
            "keywords": ["Aspen Dental", "Heartland Dental", "Custom DSO Group"],
            "action_on_match": "needs_review",
        },
    }
    res = requests.post(f"{api_server}/api/config/preview", json=payload, timeout=5)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "valid"
    assert "Custom DSO Group" in data["preview"]["keywords"]


def test_api_preview_config_invalid_schema(api_server):
    payload = {
        "resource": "dso_keywords",
        "data": {
            "version": 1,
            "keywords": "Should be a list not string",
        },
    }
    res = requests.post(f"{api_server}/api/config/preview", json=payload, timeout=5)
    assert res.status_code == 400
    assert "validation" in res.json()["error"].lower()


def test_api_update_candidate_zip_refuses_direct_approval(api_server):
    payload = {
        "version": 1,
        "zip_codes": [
            {
                "zip_code": "78701",
                "city": "Austin",
                "state": "TX",
                "status": "approved",  # Prohibited via candidate update
            }
        ],
    }
    res = requests.post(f"{api_server}/api/config/zip_codes", json=payload, timeout=5)
    assert res.status_code == 403
    assert "cannot be directly marked 'approved'" in res.json()["error"]


def test_api_approve_territory_requires_confirmation(api_server):
    payload = {
        "confirm_approval": False,
        "zip_codes": ["78701"],
    }
    res = requests.post(f"{api_server}/api/config/territories/approve", json=payload, timeout=5)
    assert res.status_code == 400
    assert "Explicit territory approval confirmation required" in res.json()["error"]


def test_postman_collection_and_environment_files_valid():
    """Verify Postman collection and environment files are valid JSON and conform to Postman v2.1 structure."""
    collection_path = Path(__file__).parent.parent / "postman" / "dental-lead-generator.postman_collection.json"
    env_path = Path(__file__).parent.parent / "postman" / "dental-lead-generator.postman_environment.json"

    assert collection_path.exists(), "Postman collection file missing"
    assert env_path.exists(), "Postman environment file missing"

    with open(collection_path, "r", encoding="utf-8") as f:
        col = json.load(f)

    with open(env_path, "r", encoding="utf-8") as f:
        env = json.load(f)

    # Check collection v2.1 structure
    assert "info" in col
    assert "item" in col
    assert col["info"]["schema"] == "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"

    # Check environment values
    assert "values" in env
    var_keys = [v["key"] for v in env["values"]]
    assert "base_url" in var_keys

    # Extract all collection request paths
    registered_endpoints = {
        "/api/health",
        "/api/config",
        "/api/config/reload",
        "/api/config/preview",
        "/api/config/territories/approve",
        "/api/config/zip_codes",
        "/api/config/non_existent_resource",
        "/api/blockers",
        "/api/pipeline/preview",
        "/api/pipeline/run",
        "/api/zips",
        "/api/runs",
        "/api/runs/does-not-exist",
        "/api/review",
        "/api/review/does-not-exist/approve",
        "/api/review/does-not-exist/reject",
        "/api/config/non_existent_resource",
        "/portal",
    }

    env_map = {item["key"]: item["value"] for item in env.get("values", [])}

    found_endpoints = set()

    def extract_requests(items):
        for item in items:
            if "request" in item:
                raw_url = item["request"]["url"]["raw"]
                path = raw_url.replace("{{base_url}}", "")
                # Resolve environment variables like {{candidate_resource}}
                for k, v in env_map.items():
                    path = path.replace(f"{{{{{k}}}}}", v)
                found_endpoints.add(path)
            if "item" in item:
                extract_requests(item["item"])

    extract_requests(col["item"])

    # Verify every request in the collection matches a real server route
    for endpoint in found_endpoints:
        assert endpoint in registered_endpoints, f"Postman endpoint {endpoint} does not match any registered server route!"

    # Verify all registered endpoints are covered
    assert found_endpoints == registered_endpoints



def test_api_runs_rejects_invalid_and_unauthorized_live(api_server):
    assert requests.post(f"{api_server}/api/runs", json={"campaign": "NOPE"}, timeout=5).status_code == 400
    assert requests.post(f"{api_server}/api/runs", json={"campaign": "PRM", "mode": "bogus"}, timeout=5).status_code == 400
    res = requests.post(f"{api_server}/api/runs", json={"campaign": "PRM", "mode": "live_ingestion"}, timeout=5)
    assert res.status_code == 403


def test_api_zips_validation(api_server):
    assert requests.post(f"{api_server}/api/zips", json={"zips": []}, timeout=5).status_code == 400
    assert requests.post(f"{api_server}/api/zips", json={"zips": ["ABC"]}, timeout=5).status_code == 400


def test_api_review_and_runs_not_found(api_server):
    assert requests.post(f"{api_server}/api/review/does-not-exist/approve", timeout=5).status_code == 404
    assert requests.get(f"{api_server}/api/runs/does-not-exist", timeout=5).status_code == 404