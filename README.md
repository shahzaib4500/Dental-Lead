# Dental Lead Generator — Postman Collection Guide

This directory contains the Postman Collection and Environment files for testing, configuring, and executing the **Wizdom Dental Marketing AI Prospecting Automation API**.

---

## 1. Files Included

- **`dental-lead-generator.postman_collection.json`**: Complete Postman Collection v2.1 containing all registered API endpoints, request schemas, success examples, failure/validation examples, and automated response verification scripts.
- **`dental-lead-generator.postman_environment.json`**: Postman environment defining `base_url` (`http://127.0.0.1:8000`) plus `target_zip`, `missing_id` and `run_id` (auto-set by the Start Run request).
- **`README.md`**: Operational guide, safety instructions, and endpoint route catalog.

---

## 2. How to Start the API Locally

The API is powered by Python's built-in standard library `http.server`:

```bash
# Activate your virtual environment
source .venv/bin/activate

# Start server using the app.py CLI
python app.py --server --host 127.0.0.1 --port 8000

# Or run src/server.py directly
python src/server.py --host 127.0.0.1 --port 8000
```

The server binds to `http://127.0.0.1:8000`.

---

## 3. How to Import and Run the Collection in Postman

1. Open Postman.
2. Click **Import** (top left).
3. Choose both `postman/dental-lead-generator.postman_collection.json` and `postman/dental-lead-generator.postman_environment.json`.
4. In the top-right environment dropdown, select **Dental Lead Generator - Local Environment**.
5. You can execute requests individually or run the entire folder via Postman Collection Runner.

---

## 4. Complete Route Inventory & Safety Classification

| Method | Endpoint | Group | Purpose | Safety Classification |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/api/health` | Health & Diagnostics | Runtime status, configuration validity, mode check | **Safe / Read-only** |
| `GET` | `/api/config` | Configuration Overview | Summary metadata across all configuration resources | **Safe / Read-only** |
| `POST` | `/api/config/reload` | Configuration Overview | Re-reads and validates all JSON configs from disk | **Safe / Local Disk Reload** |
| `GET` | `/api/blockers` | Configuration Overview | Lists registered blockers, toggle status, and severity | **Safe / Read-only** |
| `GET` | `/api/config/<resource>` | Lead Config CRUD | Returns raw JSON for an allowlisted resource (e.g. `zip_codes`) | **Safe / Read-only** |
| `POST` / `PUT` | `/api/config/<resource>` | Lead Config CRUD | Updates and validates local configuration file | ⚠️ **Local File Edit** (Validates schema; candidates cannot approve territory) |
| `POST` | `/api/config/preview` | Lead Config CRUD | Dry-run schema validation of proposed config without saving | **Safe / No File Writes** |
| `POST` | `/api/config/territories/approve` | Lead Config CRUD | Deliberate promotion of candidate ZIPs to approved production territory | ⚠️ **Local File Edit** (Requires `confirm_approval: true`) |
| `POST` | `/api/pipeline/preview` | Pipeline Preview | Dry-run normalization, deduplication, and qualification of mock leads | **Safe / Zero Network Writes** |
| `POST` | `/api/pipeline/run` | Pipeline Run | Triggers Apify discovery search across location/keywords (`search_scope`: `zip`/`city`) | ⚠️ **Discovery Action** (Guarded dry_run default; live ingestion blocked) |
| `PUT` | `/api/config/<resource>` | Lead Config CRUD | Same as POST update | ⚠️ **Local File Edit** |
| `POST` | `/api/zips` | Runs, ZIPs & Review | Registers 5-digit candidate ZIPs (never approved) | ⚠️ **Local File Edit** |
| `POST` | `/api/runs` | Runs, ZIPs & Review | Portal discovery run; accepts `mode`, `keywords`, `search_scope` | ⚠️ **Discovery Action** (always send `mode: dry_run`; omitted `mode` uses server `EXECUTION_MODE`; `live_ingestion` → 403 unless authorized) |
| `GET` | `/api/runs` | Runs, ZIPs & Review | Recent runs + recent search errors | **Safe / Read-only** |
| `GET` | `/api/runs/<id>` | Runs, ZIPs & Review | Run status and metrics (404 if unknown) | **Safe / Read-only** |
| `GET` | `/api/review` | Runs, ZIPs & Review | Pending Needs-Review leads | **Safe / Read-only** |
| `POST` | `/api/review/<id>/approve` | Runs, ZIPs & Review | Approve lead → creates Airtable record (only if live ingestion enabled) | ⚠️ **Airtable Write** |
| `POST` | `/api/review/<id>/reject` | Runs, ZIPs & Review | Reject lead (local DB only) | **Local DB Write** |
| `GET` | `/portal` | Portal | Static review dashboard (HTML) | **Safe / Read-only** |

---

---

## 5. Ready-to-Run cURL Commands for Every Endpoint

The following commands work directly against the local server at `http://127.0.0.1:8000`.

### Group 1: Health & Diagnostics

#### 1. `GET /api/health`
Check server runtime status, execution mode, and configuration validation.
```bash
curl -X GET http://127.0.0.1:8000/api/health \
  -H "Accept: application/json"
```

---

### Group 2: Configuration Overview & Reload

#### 2. `GET /api/config`
Retrieve summary metadata across all 8 configuration resources.
```bash
curl -X GET http://127.0.0.1:8000/api/config \
  -H "Accept: application/json"
```

#### 3. `POST /api/config/reload`
Hot-reload and validate all JSON configuration files from disk.
```bash
curl -X POST http://127.0.0.1:8000/api/config/reload \
  -H "Accept: application/json"
```

#### 4. `GET /api/blockers`
List all registered blocker engine definitions, active status, and actions.
```bash
curl -X GET http://127.0.0.1:8000/api/blockers \
  -H "Accept: application/json"
```

---

### Group 3: Lead Configuration CRUD

#### 5. `GET /api/config/<resource>`
Fetch raw JSON for an allowlisted configuration resource (`zip_codes`, `dso_keywords`, `territories`, `excluded_chains`, `existing_referrers`, `do_not_contact`, `qualification_rules`, `blockers`).

*Example: Inspect Texas candidate ZIP code registry:*
```bash
curl -X GET http://127.0.0.1:8000/api/config/zip_codes \
  -H "Accept: application/json"
```

*Example: Inspect DSO keywords:*
```bash
curl -X GET http://127.0.0.1:8000/api/config/dso_keywords \
  -H "Accept: application/json"
```

#### 6. `POST /api/config/preview`
Dry-run schema validation of proposed configuration without persisting to disk.
```bash
curl -X POST http://127.0.0.1:8000/api/config/preview \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "resource": "zip_codes",
    "data": {
      "version": 1,
      "state_focus": "TX",
      "zip_codes": [
        {
          "zip_code": "78701",
          "city": "Austin",
          "state": "TX",
          "status": "candidate"
        }
      ]
    }
  }'
```

#### 7. `POST /api/config/<resource>` (Update Resource)
Update an allowlisted configuration resource with strict schema validation. Candidate ZIP updates cannot directly mark entries as `approved`.
```bash
curl -X POST http://127.0.0.1:8000/api/config/zip_codes \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "version": 1,
    "description": "Registry of candidate ZIP codes for Texas territory research.",
    "state_focus": "TX",
    "zip_codes": [
      {
        "zip_code": "78701",
        "city": "Austin",
        "state": "TX",
        "county": "Travis",
        "status": "candidate",
        "source": "USPS Directory 2026",
        "notes": "Downtown Austin candidate"
      },
      {
        "zip_code": "78702",
        "city": "Austin",
        "state": "TX",
        "county": "Travis",
        "status": "candidate",
        "source": "USPS Directory 2026",
        "notes": "East Austin candidate"
      }
    ]
  }'
```

#### 8. `POST /api/config/territories/approve`
Promote candidate ZIP codes to approved production territory. Requires explicit `"confirm_approval": true`.
```bash
curl -X POST http://127.0.0.1:8000/api/config/territories/approve \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "zip_codes": ["78701", "78702"],
    "confirm_approval": true
  }'
```

---

### Group 4: Pipeline Preview & Discovery

#### 9. `POST /api/pipeline/preview`
Offline dry-run normalization, deduplication, and qualification of mock lead records without scraping or Airtable writes.
```bash
curl -X POST http://127.0.0.1:8000/api/pipeline/preview \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "campaign": "PRM",
    "leads": [
      {
        "title": "Austin Metro Family Dental",
        "categoryName": "General dentist",
        "phone": "+1 512-555-0101",
        "website": "https://austinmetrofamilydental.example",
        "address": "100 Congress Ave, Austin, TX 78701",
        "placeId": "ChIJ_austin_1"
      },
      {
        "title": "Aspen Dental Partners",
        "categoryName": "Dentist",
        "phone": "+1 512-555-0200",
        "website": "https://aspendental.example",
        "address": "200 Congress Ave, Austin, TX 78701",
        "placeId": "ChIJ_aspen_2"
      }
    ]
  }'
```

#### 10. `POST /api/pipeline/run` (Discovery Execution - Guarded Dry Run)
Executes Apify Google Maps actor discovery search. Defaults safely to `mode: "dry_run"` (zero Airtable writes).
```bash
curl -X POST http://127.0.0.1:8000/api/pipeline/run \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "campaign": "PRM",
    "location": "Austin, TX",
    "max_results": 5,
    "mode": "dry_run"
  }'
```

---

## 6. Candidate Territories vs. Approved Territories

- **Initial Candidate State**: **Texas (TX)** is configured as the candidate research state in `config/zip_codes.json`.
- **Candidate Non-Approval**: Candidate ZIP codes (e.g. `78701`, `78702`, `75201`) **NEVER** qualify a lead for ingestion into Airtable.
- **Fail-Closed Protection**: If `config/territories.json` has empty approved lists, all leads trigger `TERRITORY_NOT_CONFIGURED` and enter `Needs Review`.
- **Deliberate Approval**: Candidate ZIP codes can only be approved through explicit business action via `POST /api/config/territories/approve` with `"confirm_approval": true`.

---

## 7. Airtable Prepared but Unintegrated

- **Status**: Airtable integration models and client classes are fully implemented and tested offline, but remote Airtable network access remains **disabled**.
- **No Credentials Required Offline**: Missing Airtable credentials do not break local normalization, qualification, deduplication, configuration CRUD, or preview pipelines.
- **Live Ingestion Gate**: Live writes (`mode=live_ingestion`) return `403 Forbidden` unless both `ENABLE_LIVE_INGESTION=true` is set and valid credentials are confirmed.

---

## 8. Automated Test Verification

All routes, validation error cases, candidate ZIP protections, and Postman collection synchronization are verified using pytest:
```bash
# Run the complete test suite
.venv/bin/pytest -v
```
- **Tests Passing**: 58/58 passed.
- **Postman Sync Test**: `test_postman_collection_and_environment_files_valid` validates that every request in the Postman collection matches an active registered endpoint and valid v2.1 JSON schema.