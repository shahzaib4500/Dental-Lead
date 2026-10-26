# Manual Testing Guide — Dental Lead Generator

Order matters: every step before Step 6 is read-only. Only Step 6 writes to Airtable.

## Credentials status: NOT added to the server yet
Until credentials are added, only the offline tests work. Do Part A now; do Part B after you add the credentials.

- **Part A, no credentials needed:** Steps 0, 1, 2, 3, 5 (config, preview, ZIP registration, review-queue 404s, portal rendering).
- **Part B, needs credentials:** Step 4 (duplicate check against Airtable), live runs in Step 5, Step 6 (live ingestion) and the Step 7 token failures.

What happens without credentials (expected, not a bug):
| Call | Behavior |
|---|---|
| `GET /api/health` | `apify_configured: false`, `airtable_configured: false` |
| `POST /api/pipeline/preview` | works (offline) |
| `POST /api/pipeline/run` / `POST /api/runs` (dry_run) | no Apify scrape and no Airtable duplicate check: `total_discovered: 0`, error listed in `GET /api/runs` → `recent_errors`. This is NOT a real duplicate test |
| `mode: live_ingestion` | 403 (not authorized) |

### How to add the credentials
Create a `.env` file in the project root (same folder as `app.py`; it is loaded automatically):
```
APIFY_API_TOKEN=<your Apify token>
AIRTABLE_API_KEY=<your Airtable personal access token>
AIRTABLE_BASE_ID=appnKumhO07VlzUe8
EXECUTION_MODE=dry_run
ENABLE_LIVE_INGESTION=false
```
Restart the server, then verify: `GET /api/health` must show `apify_configured: true` and `airtable_configured: true`. Do not commit `.env`. Keep `ENABLE_LIVE_INGESTION=false` until Step 6.

## Live API calls: what is real in each mode (once credentials are added)
Apify scraping and Airtable reads (approved ZIPs, DNC list, duplicate check) are **live in every mode** as long as credentials are in `.env`. The mode only controls Airtable **writes**.

| Mode | Apify scrape | Airtable reads | Airtable writes |
|---|---|---|---|
| `dry_run` | live | live | none |
| `live_discovery` | live | live | none |
| `live_ingestion` | live | live | yes (Qualified + Unique only; needs `ENABLE_LIVE_INGESTION=true`) |

Rules for live testing:
- Keep `.env` on `EXECUTION_MODE=dry_run` and `ENABLE_LIVE_INGESTION=false`, and **always pass `"mode"` in each request**. A request with no `mode` uses the `.env` value, including the portal's Start Discovery Run button.
- Keep `max_results` at 3–5. Every keyword is a billed Apify call.
- Confirm every live response shows the expected `execution_mode` and `ingested_count: 0`.

## 0. Setup
```bash
cd <project>
source .venv/bin/activate && pip install -r requirements.txt
# .env: no credentials yet, so start safe (add them later, see "How to add the credentials"):
EXECUTION_MODE=dry_run
ENABLE_LIVE_INGESTION=false
python app.py --server --host 127.0.0.1 --port 8000
```

## 1. Automated baseline (no credentials needed)
```bash
env APIFY_API_TOKEN= AIRTABLE_API_KEY= EXECUTION_MODE=dry_run ENABLE_LIVE_INGESTION=false python -m pytest -q
```
Expect: `58 passed`.

## 2. Health + config (Postman folders 1–2, or curl). Part A
Before credentials, `apify_configured` and `airtable_configured` are `false`; after Part B setup they must be `true`.

| Request | Expect |
|---|---|
| `GET /api/health` | 200, `config_status: valid`, `enable_live_ingestion: false`, `apify_configured: true`, `airtable_configured: true` |
| `GET /api/config` | 200, no API keys anywhere in the body |
| `POST /api/config/reload` | 200 |
| `GET /api/blockers` | 200, `DUPLICATE_LEAD` present and enabled |

## 3. Preview (offline, zero side effects)
Run **Preview Qualification & Blockers**. Expect 200, DSO lead (Aspen Dental) blocked, `eligible_for_ingestion` matches your territory config.

## 4. Duplicate check against Airtable (the key manual test). Part B, needs credentials
1. Open Airtable → Practice Pipeline. Pick one existing record. Note Name, Phone, Website, Address, Zipcode.
2. `POST /api/pipeline/preview` is OFFLINE (no Airtable), so use a live read-only run instead (real Apify scrape + real Airtable duplicate check, no writes):
```bash
curl -s -X POST localhost:8000/api/pipeline/run -H 'Content-Type: application/json' \
 -d '{"campaign":"PRM","location":"<ZIP of that record>","max_results":5,"mode":"live_discovery"}' | python -m json.tool
```
   Check `execution_mode: "live_discovery"`, `summary.total_discovered > 0` and `ingested_count: 0`. If discovered is 0, look at `GET /api/runs` → `recent_errors` (bad token / Apify error).
3. In the response, find that practice in `leads[]`. Expect:
   - `duplicate_status: "Confirmed Duplicate"`, `qualification_status: "Duplicate"`
   - `triggered_blocker_ids` contains `DUPLICATE_LEAD`, `ingestion_permitted: false`
   - `qualification_reason` contains the Airtable `rec...` id
4. Brand-new practice → `duplicate_status: "Unique"`.
5. Format check: pick a record whose Address is street-only; it must still match the Apify full address.
6. Same practice found twice in one run (two keywords) → first is Unique, repeat is Duplicate.
7. **Fail-closed test:** temporarily set a wrong `AIRTABLE_API_KEY`, then:
```bash
curl -s -X POST localhost:8000/api/pipeline/run -H 'Content-Type: application/json' \
 -d '{"campaign":"PRM","location":"91423","mode":"live_ingestion"}'
```
   Expect 403 or 500 "could not be loaded for duplicate checking". Never 200 with ingestion. Restore the key afterwards.

## 5. Runs, ZIPs, review queue, portal (ZIP, 404 and portal checks work now; live runs need credentials)
- `POST /api/zips {"zips":["91423"]}` → 200; `{"zips":["ABC"]}` → 400; `{"zips":[]}` → 400.
- `POST /api/runs` with `"mode":"live_discovery"` (live scrape, no writes) → 200, `summary.ingested: 0`; `GET /api/runs` lists it; `GET /api/runs/<run_id>` → 200; unknown id → 404.
- Open http://127.0.0.1:8000/portal: pending leads appear; Reject one (disappears); Approve one (see Step 6 for the write).
- Portal safety: a lead name like `<img src=x onerror=alert(1)>` must display as text, not execute.

## 6. Live ingestion (ONE controlled write). Part B, needs credentials
Only after steps 1–5 pass.
```bash
./run_live_tests.sh --ingest        # type INGEST when prompted
```
or manually: set BOTH `EXECUTION_MODE=live_ingestion` and `ENABLE_LIVE_INGESTION=true` in `.env` (if either is off the server returns 403), restart the server, then
```bash
curl -s -X POST localhost:8000/api/pipeline/run -H 'Content-Type: application/json' \
 -d '{"campaign":"PRM","location":"91423","max_results":3,"mode":"live_ingestion"}'
```
Verify in Airtable:
- New rows exist only for `Qualified` + `Unique` leads; fields look right (Name, Phone, Website, Address, Zipcode, Practice Type, Lead Type, Qualified?, Prospect Date, Prospect Log).
- Nothing written for Duplicate / DNC / DSO leads.
- **Re-run the same command → `ingested_count: 0`** (dedupe works).
- Delete the test rows afterwards. Set the env flags back to `dry_run` / `false` and restart, so the portal button can never write by default.

## 7. Failure cases
- Invalid campaign / mode → 400. `live_ingestion` with flag off → 403.
- Stop the network or use a bad Apify token → run completes with `recent_errors` in `GET /api/runs`, 0 ingested.

## Postman note
The collection's Run Pipeline and Start Discovery Run requests already send `mode`. They make live Apify/Airtable-read calls even with `dry_run`; change the body to `"mode": "live_discovery"` if you want the explicit label. Keep `max_results` small.