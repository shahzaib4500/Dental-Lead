# Territory Management Guide & Operational Runbook

This guide explains how sales territories are structured, validated, researched, and approved in the **Wizdom Dental Marketing AI Prospecting Automation**.

---

## 1. Safety Architecture: Fail-Closed Protection

1. **Automatic Ingestion Gate**: Discovered leads **cannot** be ingested into Airtable unless they fall within an **explicitly approved** territory (ZIP code, city, or state) in `config/territories.json`.
2. **Candidate vs. Approved Separation**:
   - `config/zip_codes.json`: Houses research-derived candidate ZIP codes and detailed audit records (`zip_code`, `city`, `state`, `county`, `status`, `source`, `notes`).
   - Entries with `"status": "candidate"` **NEVER** qualify a lead for automatic ingestion. They are marked for review.
   - `config/territories.json`: Houses the active, authoritative production lists (`approved_zip_codes`, `approved_cities`, `approved_states`).
   - If `config/territories.json` contains empty approved lists and `require_approved_territory: true`, all leads trigger the `TERRITORY_NOT_CONFIGURED` blocker and receive status `Needs Review`.
3. **Leading Zeros**: ZIP codes must **always** be formatted as strings (e.g. `"07001"`, `"08540"`). Numerical formats drop leading zeros and cause lookup failures.
4. **Candidate State Focus: Texas (TX)**:
   - Initial candidate research focus is configured for **Texas (TX)**.
   - `config/zip_codes.json` contains a curated non-exhaustive research sample of 10 key Texas ZIP codes across Austin, Dallas, Houston, San Antonio, Fort Worth, and Plano.
   - **None of these ZIP codes are marked as approved production territory.**
   - All 10 candidate entries remain strictly in `status: candidate` pending business authorization.

---

## 2. File Formats & Schemas

### `config/territories.json` (Authoritative Production Filter)
```json
{
  "version": 1,
  "description": "Territory qualification configuration. Only explicitly approved ZIP codes, cities, or states qualify leads for automatic ingestion.",
  "approved_zip_codes": [],
  "approved_cities": [],
  "approved_states": [],
  "require_approved_territory": true
}
```
*Note: An empty list of approved entities means the pipeline fails closed: zero leads can pass to live ingestion.*

### `config/zip_codes.json` (Candidate & Historical Research Registry)
```json
{
  "version": 1,
  "description": "Registry of candidate and approved ZIP codes for territory qualification. Only entries with status 'approved' in config/territories.json qualify leads for automatic ingestion.",
  "state_focus": "TX",
  "zip_codes": [
    {
      "zip_code": "78701",
      "city": "Austin",
      "state": "TX",
      "county": "Travis",
      "status": "candidate",
      "source": "USPS ZIP Code Directory / Texas State Candidate Sample (2026-10)",
      "notes": "Downtown Austin metro candidate - non-exhaustive sample - pending business authorization"
    }
  ]
}
```

---

## 3. Why Candidates Cannot Pass the Ingestion Gate

- Ingestion safety gates in `LeadPipeline._enforce_mandatory_safety()` enforce that only leads with status `Qualified` can ever be queued for live ingestion.
- The qualification engine inspects `config/territories.json`. If a ZIP code is only present in `config/zip_codes.json` as a candidate, the territory check fails.
- Even if an operator disables optional blockers in `config/blockers.json`, the mandatory safety gate explicitly checks for unapproved or candidate territories and rejects them.

---

## 4. How to Approve a Territory Deliberately

To promote candidate ZIP codes to active production territory:

### Method A: Via HTTP API (Recommended)
Send a POST request to `/api/config/territories/approve`:
```http
POST /api/config/territories/approve HTTP/1.1
Content-Type: application/json

{
  "confirm_approval": true,
  "zip_codes": ["78701", "78702"]
}
```
*The API validates string types, prevents numeric truncation, updates `config/territories.json` atomically, and reloads active configuration.*

### Method B: Manual Configuration
1. Open `config/territories.json`.
2. Add the approved 5-digit ZIP string(s) to `"approved_zip_codes"`.
3. If running the API server, trigger `POST /api/config/reload` to apply changes without restarting the process.
