#!/usr/bin/env bash
# =============================================================================
# Dental Lead Generator - full test run with REAL Apify + Airtable calls
#
# Run from the project root (the folder containing app.py and .env):
#     chmod +x run_live_tests.sh
#     ./run_live_tests.sh                 # safe: Apify scrape + Airtable READS only
#     ./run_live_tests.sh --ingest        # also writes Qualified leads to Airtable
#
# Phases
#   0  Environment + credential checks (keys are never printed)
#   1  Offline unit tests (pytest)
#   2  Config validation + pilot accuracy benchmark
#   3  Airtable live, read-only (credentials, schema preflight, table reads)
#   4  Apify live (token check + small real Google Maps scrape, dry-run)
#   5  HTTP API server end-to-end (incl. live pipeline run, still dry-run)
#   6  Optional live ingestion (only with --ingest, asks for confirmation)
#
# Cost control: every Apify run is capped by MAX_RESULTS (default 3).
# Output + logs land in ./test_results/<timestamp>/
# =============================================================================

set -u
cd "$(dirname "$0")"

# ---- settings (override via environment) ------------------------------------
PY="${PYTHON:-python3}"
ZIP_CODE="${ZIP_CODE:-91423}"
CITY="${CITY:-Sherman Oaks, CA}"
MAX_RESULTS="${MAX_RESULTS:-3}"
PORT="${PORT:-$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')}"
BASE="http://127.0.0.1:${PORT}"
INGEST=0
[[ "${1:-}" == "--ingest" ]] && INGEST=1

TS="$(date +%Y%m%d_%H%M%S)"
OUT="test_results/${TS}"
mkdir -p "$OUT"

PASS=0; FAIL=0; SKIP=0; CALLS=0
SERVER_PID=""

# ---- helpers ----------------------------------------------------------------
c_green=$'\033[32m'; c_red=$'\033[31m'; c_yel=$'\033[33m'; c_off=$'\033[0m'
phase() { echo; echo "================================================================"; echo "  $*"; echo "================================================================"; }
ok()    { echo "${c_green}PASS${c_off}  $*"; PASS=$((PASS+1)); }
bad()   { echo "${c_red}FAIL${c_off}  $*"; FAIL=$((FAIL+1)); }
skip()  { echo "${c_yel}SKIP${c_off}  $*"; SKIP=$((SKIP+1)); }

# run "<label>" <logfile> <cmd...>   -> PASS/FAIL on exit code
run() {
  local label="$1" log="$2"; shift 2
  if "$@" >"$OUT/$log" 2>&1; then ok "$label  (log: $OUT/$log)"; return 0
  else bad "$label  (log: $OUT/$log)"; tail -n 15 "$OUT/$log" | sed 's/^/        /'; return 1; fi
}

# http_check "<label>" <expected_status> <METHOD> <path> [json-body] [must_contain]
http_check() {
  local label="$1" want="$2" method="$3" path="$4" body="${5:-}" must="${6:-}"
  CALLS=$((CALLS+1))
  local file="$OUT/api_$(printf '%02d' "$CALLS")_$(echo "${method}_${path}" | tr '/?&=' '____').json"
  local code
  if [[ -n "$body" ]]; then
    code=$(curl -s -o "$file" -w '%{http_code}' -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body" --max-time 900)
  else
    code=$(curl -s -o "$file" -w '%{http_code}' -X "$method" "$BASE$path" --max-time 900)
  fi
  if [[ "$code" != "$want" ]]; then
    bad "$label  [$method $path -> $code, expected $want]  (body: $file)"; head -c 400 "$file" | sed 's/^/        /'; echo
  elif [[ -n "$must" ]] && ! grep -qi -- "$must" "$file"; then
    bad "$label  [$method $path -> $code but body lacks '$must' - generic 404 from wrong server?]  (body: $file)"; head -c 300 "$file" | sed 's/^/        /'; echo
  else
    ok "$label  [$method $path -> $code]"
  fi
}

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

# =============================================================================
phase "PHASE 0 - Environment"
# =============================================================================
if [[ ! -f .env ]]; then bad ".env not found in $(pwd)"; exit 1; fi
set -a; . ./.env; set +a    # export so curl/jq checks see the same values python-dotenv loads

mask() { local v="${1:-}"; [[ -z "$v" ]] && echo "<missing>" || echo "${v:0:4}...(${#v} chars)"; }
echo "APIFY_API_TOKEN : $(mask "${APIFY_API_TOKEN:-}")"
echo "AIRTABLE_API_KEY: $(mask "${AIRTABLE_API_KEY:-}")"
echo "AIRTABLE_BASE_ID: ${AIRTABLE_BASE_ID:-appnKumhO07VlzUe8 (default)}"
echo "EXECUTION_MODE  : ${EXECUTION_MODE:-dry_run (default)}   <- from .env; this script forces dry_run for phases 1-5"
echo "ENABLE_LIVE_INGESTION: ${ENABLE_LIVE_INGESTION:-false (default)}"

[[ -n "${APIFY_API_TOKEN:-}"  ]] && ok "APIFY_API_TOKEN present"  || bad "APIFY_API_TOKEN missing in .env"
[[ -n "${AIRTABLE_API_KEY:-}" ]] && ok "AIRTABLE_API_KEY present" || bad "AIRTABLE_API_KEY missing in .env"
[[ $FAIL -gt 0 ]] && { echo "Fix .env first (the app silently 'skips' Airtable checks when keys are missing)."; exit 1; }

# Force safe defaults for every phase except the optional ingest phase
export EXECUTION_MODE=dry_run
export ENABLE_LIVE_INGESTION=false

if [[ ! -d .venv ]]; then "$PY" -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
run "pip install requirements" 00_pip.log pip install -q -r requirements.txt
PY=python

# =============================================================================
phase "PHASE 1 - Offline unit tests"
# =============================================================================
run "pytest (full suite, offline: credentials blanked)" 01_pytest.log \
  env APIFY_API_TOKEN= AIRTABLE_API_KEY= EXECUTION_MODE=dry_run ENABLE_LIVE_INGESTION=false "$PY" -m pytest -v --tb=short
tail -n 5 "$OUT/01_pytest.log"

# =============================================================================
phase "PHASE 2 - Config validation + pilot benchmark"
# =============================================================================
run "load + validate all 7 JSON configs" 02_config.log "$PY" -c "
from src.config_loader import ConfigLoader
b = ConfigLoader().load_all()
print('configs loaded OK')"
run "pilot accuracy benchmark (fixtures)" 02_pilot.log "$PY" scripts/pilot_report.py
grep -E "Accuracy|Escalations" "$OUT/02_pilot.log" | sed 's/^/        /'

# =============================================================================
phase "PHASE 3 - Airtable LIVE (read-only)"
# =============================================================================
run "Airtable credential format check" 03_at_creds.log "$PY" -c "
import sys, json
from src.airtable_client import AirtableClient
r = AirtableClient().validate_credentials()
print(json.dumps(r, indent=2))
sys.exit(0 if r['valid'] else 1)"

# raw API call: proves the key + base id actually work against Airtable
code=$(curl -s -o "$OUT/03_at_meta.json" -w '%{http_code}' \
  -H "Authorization: Bearer ${AIRTABLE_API_KEY}" \
  "https://api.airtable.com/v0/meta/bases/${AIRTABLE_BASE_ID:-appnKumhO07VlzUe8}/tables")
if [[ "$code" == "200" ]]; then
  ok "Airtable Meta API reachable, base readable  [200]"
  "$PY" - <<'EOF'
import json, glob
d = json.load(open(sorted(glob.glob("test_results/*/03_at_meta.json"))[-1]))
print("        tables in base:", ", ".join(t["name"] for t in d["tables"]))
EOF
elif [[ "$code" == "403" || "$code" == "401" ]]; then
  skip "Meta API returned $code (token lacks schema.bases:read) - preflight will fall back to sample-record mode"
else
  bad "Airtable Meta API  [$code]"; head -c 300 "$OUT/03_at_meta.json"; echo
fi

run "Airtable schema preflight (app.py --preflight)" 03_at_preflight.log "$PY" app.py --preflight
grep -E '"status"|discrepanc|mismatch|missing' "$OUT/03_at_preflight.log" | head -10 | sed 's/^/        /'
if grep -q '"status": "skipped"' "$OUT/03_at_preflight.log"; then
  bad "preflight was SKIPPED (credentials treated as placeholders) - not a real pass"
fi

run "read approved ZIPs + DNC list from Airtable" 03_at_reads.log "$PY" -c "
from src.airtable_client import AirtableClient
c = AirtableClient()
zips, zmap = c.load_approved_zipcodes()
dnc = c.load_dnc_and_referrers()
print(f'approved zips from Airtable: {len(zips)}  e.g. {zips[:5]}')
print(f'DNC / referrer entries:      {len(dnc)}')
assert isinstance(zips, list)"
grep -E "approved zips|DNC" "$OUT/03_at_reads.log" | sed 's/^/        /'

# =============================================================================
phase "PHASE 4 - Apify LIVE (real scrape, dry-run, max ${MAX_RESULTS} results)"
# =============================================================================
code=$(curl -s -o "$OUT/04_apify_me.json" -w '%{http_code}' -H "Authorization: Bearer ${APIFY_API_TOKEN}" "https://api.apify.com/v2/users/me")
[[ "$code" == "200" ]] && ok "Apify token valid  [200]" || { bad "Apify token check  [$code]"; head -c 300 "$OUT/04_apify_me.json"; echo; }

code=$(curl -s -o "$OUT/04_apify_actor.json" -w '%{http_code}' -H "Authorization: Bearer ${APIFY_API_TOKEN}" \
  "https://api.apify.com/v2/acts/${APIFY_ACTOR_ID:-nwua9Gu5YrADL7ZDj}")
[[ "$code" == "200" ]] && ok "Apify actor ${APIFY_ACTOR_ID:-nwua9Gu5YrADL7ZDj} accessible  [200]" || bad "Apify actor lookup  [$code]"

run "PRM dry-run, ZIP ${ZIP_CODE}, scope=zip" 04_prm_zip.log \
  "$PY" app.py --campaign PRM --zip "$ZIP_CODE" --search-scope zip --max-results "$MAX_RESULTS" --dry-run --output "$OUT/04_prm_zip_results.json"

run "WISDOM_SALES dry-run, ${CITY}, scope=city" 04_ws_city.log \
  "$PY" app.py --campaign WISDOM_SALES --location "$CITY" --search-scope city --max-results "$MAX_RESULTS" --dry-run --output "$OUT/04_ws_city_results.json"

run "custom keyword dry-run (orthodontist)" 04_custom_kw.log \
  "$PY" app.py --campaign WISDOM_SALES --zip "$ZIP_CODE" --keywords "oral surgeon" --max-results "$MAX_RESULTS" --dry-run --output "$OUT/04_kw_results.json"

# sanity-check the saved results: Apify must really have returned leads, and nothing may be written to Airtable.
# (the pipeline swallows Apify errors per keyword, so exit code 0 alone does not prove the scrape worked)
if "$PY" - "$OUT" <<'PYEOF'
import json, sys, glob
bad = 0
files = sorted(glob.glob(f"{sys.argv[1]}/04_*_results.json"))
if not files: print("        !! no result files saved - dry-run did not finish"); sys.exit(1)
for f in files:
    d = json.load(open(f)); s = d["summary"]
    problems = []
    if s["total_discovered"] == 0: problems.append("0 leads discovered (Apify error / bad token / no results - see GET /api/runs recent_errors)")
    if d["ingested_count"] != 0: problems.append("records were ingested in a dry-run!")
    flag = "!! " if problems else "OK "
    print(f"        {flag}{f.split('/')[-1]}: discovered={s['total_discovered']} qualified={s['qualified']} "
          f"review={s['needs_review']} disq={s['disqualified']} dup={s.get('duplicates', s.get('duplicate', 0))} "
          f"dnc={s['existing_referrer_or_dnc']} ingested={d['ingested_count']}")
    for p in problems: print("           ->", p)
    bad += bool(problems)
sys.exit(1 if bad else 0)
PYEOF
then ok "Apify returned leads and dry-runs wrote nothing to Airtable"
else bad "Apify result sanity check failed (see lines above)"; fi

# =============================================================================
phase "PHASE 5 - HTTP API end-to-end (server on :${PORT})"
# =============================================================================
"$PY" app.py --server --host 127.0.0.1 --port "$PORT" >"$OUT/05_server.log" 2>&1 &
SERVER_PID=$!
for i in $(seq 1 40); do
  curl -s "$BASE/api/health" >/dev/null 2>&1 && break
  kill -0 "$SERVER_PID" 2>/dev/null || break      # server process died (e.g. port in use)
  sleep 0.5
done
sleep 1
if kill -0 "$SERVER_PID" 2>/dev/null && curl -s "$BASE/api/health" >/dev/null 2>&1; then
  ok "server started on :${PORT} (pid $SERVER_PID)"
else
  bad "server failed to start on :${PORT}"; tail -20 "$OUT/05_server.log"; exit 1
fi
# Guard: make sure THIS code version has the review/run routes (not an old server / old checkout)
guard=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/review")
if [[ "$guard" != "200" ]]; then
  bad "server on :${PORT} has no GET /api/review (got $guard) - old src/server.py? Compare with the postman collection."
  exit 1
fi

# read-only / offline endpoints
http_check "health"                      200 GET  /api/health
http_check "config summary"              200 GET  /api/config
http_check "config reload"               200 POST /api/config/reload '{}'
http_check "blockers list"               200 GET  /api/blockers
http_check "get zip_codes resource"      200 GET  /api/config/zip_codes
http_check "review queue"                200 GET  /api/review
http_check "runs list"                   200 GET  /api/runs

# secrets must never leak through the API
if grep -qE "${APIFY_API_TOKEN:0:8}|${AIRTABLE_API_KEY:0:8}" "$OUT"/api_*_GET__api_health.json "$OUT"/api_*_GET__api_config.json 2>/dev/null; then
  bad "API responses leak credentials"; else ok "no credentials leaked in /api/health or /api/config"; fi

# offline preview with a mock lead (no scrape, no Airtable)
http_check "pipeline preview (mock lead)" 200 POST /api/pipeline/preview \
  '{"campaign":"PRM","leads":[{"title":"Sunrise Family Dental","phone":"(818) 555-0101","website":"https://sunrisefamilydental.example.com","address":"123 Ventura Blvd, Sherman Oaks, CA 91423","postalCode":"91423","categoryName":"Dentist","placeId":"TEST_PLACE_1"}]}'

# validation + safety guards
http_check "bad campaign rejected"        400 POST /api/pipeline/run '{"campaign":"NOPE"}'
http_check "bad mode rejected"            400 POST /api/pipeline/run '{"campaign":"PRM","mode":"nope"}'
http_check "live_ingestion blocked (flag off)" 403 POST /api/pipeline/run '{"campaign":"PRM","mode":"live_ingestion"}'
http_check "add candidate ZIPs (validation)" 400 POST /api/zips '{"zips":[]}' 'non-empty list'
http_check "unknown review id -> 404"     404 POST /api/review/does-not-exist/reject '{}' 'Review lead'
http_check "unknown run id -> 404"        404 GET  /api/runs/does-not-exist '' 'not found'

# LIVE: real Apify scrape through the API, dry-run mode, capped
http_check "LIVE pipeline/run PRM dry-run"  200 POST /api/pipeline/run \
  "{\"campaign\":\"PRM\",\"location\":\"${ZIP_CODE}\",\"max_results\":${MAX_RESULTS},\"mode\":\"dry_run\"}"
http_check "LIVE pipeline/run WISDOM dry-run" 200 POST /api/pipeline/run \
  "{\"campaign\":\"WISDOM_SALES\",\"location\":\"${ZIP_CODE}\",\"max_results\":${MAX_RESULTS},\"mode\":\"dry_run\"}"
http_check "LIVE live_discovery (Airtable dedupe reads, no writes)" 200 POST /api/pipeline/run \
  "{\"campaign\":\"PRM\",\"location\":\"${ZIP_CODE}\",\"max_results\":${MAX_RESULTS},\"mode\":\"live_discovery\"}"
http_check "LIVE POST /api/runs"            200 POST /api/runs \
  "{\"campaign\":\"PRM\",\"zip\":\"${ZIP_CODE}\",\"max_results\":${MAX_RESULTS}}"

http_check "runs list after live runs"     200 GET  /api/runs
http_check "review queue after live runs"  200 GET  /api/review

# approve/reject one real review lead if any exist (reject is local-DB only: safe)
LEAD_ID=$("$PY" -c "
import json
try:
    import glob; f = sorted(glob.glob('$OUT/api_*_GET__api_review.json'))[-1]
    d = json.load(open(f)); l = d.get('leads') or []
    print(l[0].get('lead_id') or l[0].get('id') or '') if l else print('')
except Exception: print('')")
if [[ -n "$LEAD_ID" ]]; then
  http_check "reject first review lead (local DB only)" 200 POST "/api/review/${LEAD_ID}/reject" '{}'
else
  skip "no pending review leads to reject"
fi

# sanity: no run wrote to Airtable
"$PY" - "$OUT" <<'EOF'
import json, sys, glob
bad = 0
for f in glob.glob(f"{sys.argv[1]}/api_*_POST__api_pipeline_run.json") + glob.glob(f"{sys.argv[1]}/api_*_POST__api_runs.json"):
    try:
        d = json.load(open(f))
        if d.get("ingested_count", 0): bad += 1; print("        !! ingested_count>0 in", f)
    except Exception: pass
print("        verified: ingested_count == 0 on all dry-run/discovery calls" if not bad else "")
sys.exit(1 if bad else 0)
EOF

cleanup; SERVER_PID=""
ok "server stopped"

# =============================================================================
phase "PHASE 6 - Live ingestion (writes to Airtable)"
# =============================================================================
if [[ $INGEST -eq 1 ]]; then
  echo "This will CREATE real records in Airtable base ${AIRTABLE_BASE_ID:-appnKumhO07VlzUe8} (Practice Pipeline)"
  echo "for up to ${MAX_RESULTS} results per keyword, Qualified leads only."
  read -r -p "Type INGEST to continue: " confirm
  if [[ "$confirm" == "INGEST" ]]; then
    EXECUTION_MODE=live_ingestion ENABLE_LIVE_INGESTION=true \
      run "live ingestion, PRM, ZIP ${ZIP_CODE}" 06_ingest.log \
      "$PY" app.py --campaign PRM --zip "$ZIP_CODE" --max-results "$MAX_RESULTS" --mode live_ingestion --output "$OUT/06_ingest_results.json"
    grep -E "Ingested|Qualified|Eligible" "$OUT/06_ingest.log" | sed 's/^/        /'
    echo "        Re-run the same command to verify duplicates are blocked on the 2nd pass:"
    EXECUTION_MODE=live_ingestion ENABLE_LIVE_INGESTION=true \
      run "re-ingest same ZIP (expect 0 new records)" 06_reingest.log \
      "$PY" app.py --campaign PRM --zip "$ZIP_CODE" --max-results "$MAX_RESULTS" --mode live_ingestion --output "$OUT/06_reingest_results.json"
    "$PY" - "$OUT" <<'EOF'
import json, sys
a = json.load(open(f"{sys.argv[1]}/06_ingest_results.json"))["ingested_count"]
b = json.load(open(f"{sys.argv[1]}/06_reingest_results.json"))["ingested_count"]
print(f"        first pass ingested={a}, second pass ingested={b}  ->", "OK (dedupe works)" if b == 0 else "CHECK: duplicates may have been written")
EOF
  else
    skip "ingestion cancelled"
  fi
else
  skip "ingestion not requested (run with --ingest to enable)"
fi

# =============================================================================
phase "SUMMARY"
# =============================================================================
echo "passed: $PASS   failed: $FAIL   skipped: $SKIP"
echo "logs + JSON outputs: $OUT/"
[[ $FAIL -eq 0 ]]