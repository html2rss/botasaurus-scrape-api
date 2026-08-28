#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${SMOKE_IMAGE_NAME:-botasaurus-api-smoke}"
CONTAINER_NAME="${SMOKE_CONTAINER_NAME:-botasaurus-api-smoke-run}"
HOST_PORT="${SMOKE_HOST_PORT:-4010}"
CONTAINER_PORT=4010
BASE_URL="http://127.0.0.1:${HOST_PORT}"
SMOKE_PROFILE="${SMOKE_PROFILE:-all}"
SMOKE_SKIP_BUILD="${SMOKE_SKIP_BUILD:-0}"

SENTRY_CONTAINER=""
SENTRY_STARTED=0

cleanup() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [[ -n "${SENTRY_CONTAINER}" ]]; then
    docker rm -f "${SENTRY_CONTAINER}" >/dev/null 2>&1 || true
  fi
}

fail() {
  echo "[smoke] ERROR: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

http_post() {
  local path="$1"
  local body="$2"

  curl -sS \
    -H 'Content-Type: application/json' \
    -d "$body" \
    -w '\n%{http_code}' \
    "${BASE_URL}${path}"
}

assert_json_key_nonempty() {
  local json="$1"
  local key="$2"

  JSON_INPUT="$json" python3 - "$key" <<'PY'
import json
import os
import sys

def lookup(obj, key):
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur

key = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
value = lookup(obj, key)
if value in (None, ""):
    raise SystemExit(1)
PY
}

assert_json_key_equals() {
  local json="$1"
  local key="$2"
  local expected="$3"

  JSON_INPUT="$json" python3 - "$key" "$expected" <<'PY'
import json
import os
import sys

def lookup(obj, key):
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur

key = sys.argv[1]
expected = sys.argv[2]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
actual = lookup(obj, key)
if actual is None:
    raise SystemExit(1)
if str(actual) != expected:
    raise SystemExit(1)
PY
}

assert_html_contains() {
  local json="$1"
  local needle="$2"

  JSON_INPUT="$json" python3 - "$needle" <<'PY'
import json
import os
import sys

needle = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
html = obj.get("html") or ""
if needle not in html:
    raise SystemExit(1)
PY
}

assert_json_attempts_gte() {
  local json="$1"
  local minimum="$2"

  JSON_INPUT="$json" python3 - "$minimum" <<'PY'
import json
import os
import sys

minimum = int(sys.argv[1])
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
attempts = int((obj.get("diagnostics") or {}).get("attempts") or 0)
if attempts < minimum:
    raise SystemExit(1)
PY
}

assert_json_key_true() {
  local json="$1"
  local key="$2"

  JSON_INPUT="$json" python3 - "$key" <<'PY'
import json
import os
import sys

def lookup(obj, key):
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur

key = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
if lookup(obj, key) is not True:
    raise SystemExit(1)
PY
}

assert_json_key_false() {
  local json="$1"
  local key="$2"

  JSON_INPUT="$json" python3 - "$key" <<'PY'
import json
import os
import sys

def lookup(obj, key):
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur

key = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
if lookup(obj, key) is not False:
    raise SystemExit(1)
PY
}

assert_json_key_int_gte() {
  local json="$1"
  local key="$2"
  local minimum="$3"

  JSON_INPUT="$json" python3 - "$key" "$minimum" <<'PY'
import json
import os
import sys

def lookup(obj, key):
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur

key = sys.argv[1]
minimum = int(sys.argv[2])
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
value = int(lookup(obj, key) or 0)
if value < minimum:
    raise SystemExit(1)
PY
}

assert_html_not_contains() {
  local json="$1"
  local needle="$2"

  JSON_INPUT="$json" python3 - "$needle" <<'PY'
import json
import os
import sys

needle = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
html = obj.get("html") or ""
if needle in html:
    raise SystemExit(1)
PY
}

wait_http_ok() {
  local url="$1"
  local deadline_s="${2:-30}"
  local interval_s="${3:-0.2}"
  local deadline
  deadline="$(python3 -c "import time; print(time.monotonic() + float('${deadline_s}'))")"
  local response health_body health_code

  while true; do
    if response=$(curl -sS -w '\n%{http_code}' "${url}" 2>/dev/null); then
      health_body="$(echo "$response" | sed '$d')"
      health_code="$(echo "$response" | tail -n1)"
      if [[ "$health_code" == "200" ]]; then
        printf '%s\n' "$health_body"
        return 0
      fi
    fi
    python3 -c "import time,sys; sys.exit(0 if time.monotonic() < float(sys.argv[1]) else 1)" "${deadline}" \
      || fail "Service did not become healthy in time (${url})"
    sleep "${interval_s}"
  done
}

wait_log_contains() {
  local container="$1"
  local needle="$2"
  local deadline_s="${3:-60}"
  local interval_s="${4:-0.2}"
  local deadline
  deadline="$(python3 -c "import time; print(time.monotonic() + float('${deadline_s}'))")"
  local logs

  while true; do
    logs="$(docker logs "${container}" 2>&1 || true)"
    if echo "$logs" | grep -Fq "${needle}"; then
      printf '%s\n' "$logs"
      return 0
    fi
    python3 -c "import time,sys; sys.exit(0 if time.monotonic() < float(sys.argv[1]) else 1)" "${deadline}" \
      || fail "Timed out waiting for log marker: ${needle}"
    sleep "${interval_s}"
  done
}

count_log_matches() {
  local logs="$1"
  local needle="$2"
  local count
  count="$(echo "$logs" | grep -Fc "${needle}" || true)"
  printf '%s\n' "${count:-0}"
}

ensure_image() {
  if [[ "${SMOKE_SKIP_BUILD}" == "1" ]]; then
    if docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
      echo "[smoke] Skipping build (SMOKE_SKIP_BUILD=1); using ${IMAGE_NAME}"
      return 0
    fi
    fail "SMOKE_SKIP_BUILD=1 but image ${IMAGE_NAME} is not present"
  fi
  echo "[smoke] Building Docker image: ${IMAGE_NAME}"
  docker build -t "${IMAGE_NAME}" .
}

start_scrape_container() {
  local -a env_args=("$@")
  local -a run_args=(
    -d
    --name "${CONTAINER_NAME}"
    --init
    --shm-size=1gb
    -p "${HOST_PORT}:${CONTAINER_PORT}"
  )
  local arg
  for arg in "${env_args[@]}"; do
    run_args+=(-e "${arg}")
  done
  echo "[smoke] Starting container: ${CONTAINER_NAME}"
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker run "${run_args[@]}" "${IMAGE_NAME}" >/dev/null
}

start_sentry_sidecar() {
  SENTRY_CONTAINER="${CONTAINER_NAME}-sentry"
  echo "[smoke] Starting Sentry sidecar: ${SENTRY_CONTAINER}"
  docker rm -f "${SENTRY_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d --name "${SENTRY_CONTAINER}" \
    -e SENTRY_DSN="https://fake-key@fake.ingest.sentry.io/12345" \
    -e SENTRY_ENVIRONMENT="smoke-test" \
    "${IMAGE_NAME}" >/dev/null
  SENTRY_STARTED=1
}

assert_sentry_initialized() {
  [[ "${SENTRY_STARTED}" == "1" ]] || fail "Sentry sidecar was not started"
  echo "[smoke] Waiting for sentry_initialized in sidecar logs"
  local sentry_logs
  sentry_logs="$(wait_log_contains "${SENTRY_CONTAINER}" "sentry_initialized environment=smoke-test" 30)"
  echo "$sentry_logs" | grep -q "fake-key" && fail "Sentry DSN secret leaked into container logs"
  docker rm -f "${SENTRY_CONTAINER}" >/dev/null 2>&1 || true
  SENTRY_CONTAINER=""
  SENTRY_STARTED=0
}

assert_health() {
  echo "[smoke] Waiting for /health"
  local health_body
  health_body="$(wait_http_ok "${BASE_URL}/health" 30 0.2)"
  assert_json_key_equals "$health_body" "status" "ok" || fail "Health status != ok"
  assert_json_key_nonempty "$health_body" "service" || fail "Health service is empty"
  assert_json_key_nonempty "$health_body" "botasaurus_version" || fail "Health botasaurus_version is empty"
}

run_contract_prewarm_off() {
  echo "[smoke] Profile: contract-prewarm-off"
  start_scrape_container
  start_sentry_sidecar
  assert_health

  echo "[smoke] Checking /scrape happy path"
  local scrape_response scrape_body scrape_code
  scrape_response="$(http_post "/scrape" '{"url":"https://example.com"}')"
  scrape_body="$(echo "$scrape_response" | sed '$d')"
  scrape_code="$(echo "$scrape_response" | tail -n1)"
  [[ "$scrape_code" == "200" ]] || fail "Expected /scrape happy path 200, got ${scrape_code}"
  assert_json_key_nonempty "$scrape_body" "html" || fail "Scrape html is empty"
  assert_html_contains "$scrape_body" "Example Domain" || fail "Scrape html missing expected marker"
  assert_json_key_nonempty "$scrape_body" "diagnostics.request_id" || fail "Missing diagnostics.request_id"
  assert_json_attempts_gte "$scrape_body" 1 || fail "Expected attempts >= 1"
  assert_json_key_equals "$scrape_body" "diagnostics.execution_tier" "http_request" || fail "Expected execution_tier=http_request"
  assert_json_key_int_gte "$scrape_body" "diagnostics.render_ms" 1 || fail "Expected render_ms > 0"
  assert_json_key_false "$scrape_body" "diagnostics.challenge.blocked" || fail "Expected challenge.blocked false"
  assert_json_key_false "$scrape_body" "diagnostics.challenge.detected" || fail "Expected challenge.detected false"

  echo "[smoke] Checking explicit strategy override"
  local strategy_response strategy_body strategy_code
  strategy_response="$(http_post "/scrape" '{"url":"https://example.com","navigation_mode":"google_get_bypass","max_retries":0}')"
  strategy_body="$(echo "$strategy_response" | sed '$d')"
  strategy_code="$(echo "$strategy_response" | tail -n1)"
  [[ "$strategy_code" == "200" ]] || fail "Expected explicit strategy scrape 200, got ${strategy_code}"
  assert_json_key_equals "$strategy_body" "diagnostics.attempts" "1" || fail "Expected attempts=1 for max_retries=0"
  assert_json_key_equals "$strategy_body" "diagnostics.strategy_used" "google_get_bypass" || fail "Expected strategy_used=google_get_bypass"

  echo "[smoke] Checking retry path + final strategy in auto mode"
  local retry_response retry_body retry_code
  retry_response="$(http_post "/scrape" '{"url":"https://example.com","navigation_mode":"auto","max_retries":2,"wait_for_selector":"#definitely-missing-selector","wait_timeout_seconds":1}')"
  retry_body="$(echo "$retry_response" | sed '$d')"
  retry_code="$(echo "$retry_response" | tail -n1)"
  [[ "$retry_code" == "502" ]] || fail "Expected retry failure 502, got ${retry_code}"
  assert_json_key_equals "$retry_body" "diagnostics.attempts" "3" || fail "Expected auto mode to attempt 3 strategies"
  assert_json_key_equals "$retry_body" "diagnostics.strategy_used" "get" || fail "Expected final strategy_used=get"
  assert_json_key_equals "$retry_body" "error_category" "navigation_error" || fail "Expected navigation_error category"

  echo "[smoke] Checking per-request isolation (no cookie leak)"
  # Use httpbingo (httpbin.org is often down). headless=true avoids xvfb hangs on the Set-Cookie redirect.
  local set_cookie_response set_cookie_code
  set_cookie_response="$(http_post "/scrape" '{"url":"https://httpbingo.org/cookies/set?isotest=1","navigation_mode":"get","max_retries":0,"headless":true}')"
  set_cookie_code="$(echo "$set_cookie_response" | tail -n1)"
  [[ "$set_cookie_code" == "200" ]] || fail "Expected cookie set scrape 200, got ${set_cookie_code}"

  local check_cookie_response check_cookie_body check_cookie_code
  check_cookie_response="$(http_post "/scrape" '{"url":"https://httpbingo.org/cookies","navigation_mode":"get","max_retries":0,"headless":true}')"
  check_cookie_body="$(echo "$check_cookie_response" | sed '$d')"
  check_cookie_code="$(echo "$check_cookie_response" | tail -n1)"
  [[ "$check_cookie_code" == "200" ]] || fail "Expected cookie check scrape 200, got ${check_cookie_code}"
  assert_html_not_contains "$check_cookie_body" "isotest" || fail "Cookie leaked across requests"

  echo "[smoke] Checking SSRF guardrail (/scrape localhost)"
  local blocked_response blocked_body blocked_code
  blocked_response="$(http_post "/scrape" '{"url":"http://localhost"}')"
  blocked_body="$(echo "$blocked_response" | sed '$d')"
  blocked_code="$(echo "$blocked_response" | tail -n1)"
  [[ "$blocked_code" == "403" ]] || fail "Expected localhost scrape 403, got ${blocked_code}"
  assert_json_key_nonempty "$blocked_body" "error" || fail "Blocked response missing error message"

  echo "[smoke] Checking execution_mode=request (fast anti-detect HTTP)"
  local request_tier_response request_tier_body request_tier_code
  request_tier_response="$(http_post "/scrape" '{"url":"https://example.com","execution_mode":"request"}')"
  request_tier_body="$(echo "$request_tier_response" | sed '$d')"
  request_tier_code="$(echo "$request_tier_response" | tail -n1)"
  [[ "$request_tier_code" == "200" ]] || fail "Expected execution_mode=request 200, got ${request_tier_code}"
  assert_json_key_equals "$request_tier_body" "diagnostics.execution_tier" "http_request" || fail "Expected execution_tier=http_request"
  assert_html_contains "$request_tier_body" "Example Domain" || fail "request tier html missing marker"

  echo "[smoke] Checking custom headers and cookies propagation"
  local header_test_response header_test_body header_test_code
  header_test_response="$(http_post "/scrape" '{"url":"https://httpbingo.org/headers","execution_mode":"request","headers":{"X-Smoke-Test":"VerifiedVal"}}')"
  header_test_body="$(echo "$header_test_response" | sed '$d')"
  header_test_code="$(echo "$header_test_response" | tail -n1)"
  [[ "$header_test_code" == "200" ]] || fail "Expected header test 200, got ${header_test_code}"
  assert_html_contains "$header_test_body" "VerifiedVal" || fail "Custom header not found in upstream response"

  echo "[smoke] Checking organic_get navigation mode"
  local organic_response organic_body organic_code
  organic_response="$(http_post "/scrape" '{"url":"https://example.com","navigation_mode":"organic_get","max_retries":0,"headless":true}')"
  organic_body="$(echo "$organic_response" | sed '$d')"
  organic_code="$(echo "$organic_response" | tail -n1)"
  [[ "$organic_code" == "200" ]] || fail "Expected organic_get scrape 200, got ${organic_code}"
  assert_json_key_equals "$organic_body" "diagnostics.strategy_used" "organic_get" || fail "Expected strategy_used=organic_get"

  echo "[smoke] Checking scroll parameter"
  local scroll_response scroll_body scroll_code
  scroll_response="$(http_post "/scrape" '{"url":"https://example.com","scroll":true,"headless":true}')"
  scroll_body="$(echo "$scroll_response" | sed '$d')"
  scroll_code="$(echo "$scroll_response" | tail -n1)"
  [[ "$scroll_code" == "200" ]] || fail "Expected scroll scrape 200, got ${scroll_code}"
  assert_json_key_equals "$scroll_body" "diagnostics.execution_tier" "browser_driver" || fail "Expected execution_tier=browser_driver for scroll"

  assert_sentry_initialized
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  echo "[smoke] Profile contract-prewarm-off PASS"
}

run_prewarm_on_warm_handoff() {
  echo "[smoke] Profile: prewarm-on-warm-handoff"
  start_scrape_container \
    "SCRAPE_PREWARM=true" \
    "SCRAPE_PREWARM_MIN_REFILL_SECONDS=0"
  assert_health

  echo "[smoke] Cold browser scrape (expect warm_hit=False)"
  local cold_response cold_body cold_code cold_logs cold_hits
  cold_response="$(http_post "/scrape" '{"url":"https://example.com","execution_mode":"browser","navigation_mode":"get","max_retries":0,"headless":true}')"
  cold_body="$(echo "$cold_response" | sed '$d')"
  cold_code="$(echo "$cold_response" | tail -n1)"
  [[ "$cold_code" == "200" ]] || fail "Expected cold browser scrape 200, got ${cold_code}"
  assert_html_contains "$cold_body" "Example Domain" || fail "Cold scrape html missing marker"
  assert_json_key_equals "$cold_body" "diagnostics.execution_tier" "browser_driver" || fail "Expected browser_driver tier"
  cold_logs="$(docker logs "${CONTAINER_NAME}" 2>&1)"
  cold_hits="$(count_log_matches "$cold_logs" "scrape_boot ")"
  [[ "${cold_hits}" -ge 1 ]] || fail "Expected at least one scrape_boot log after cold scrape"
  echo "$cold_logs" | grep -Fq "scrape_boot " || fail "Missing scrape_boot log"
  echo "$cold_logs" | grep -E "scrape_boot .*warm_hit=False" >/dev/null \
    || fail "Expected scrape_boot warm_hit=False after cold scrape"
  echo "$cold_logs" | grep -E "scrape_boot .*warm_hit=True" >/dev/null \
    && fail "Unexpected warm_hit=True before refill"

  echo "[smoke] Waiting for warm_pool_refill"
  wait_log_contains "${CONTAINER_NAME}" "warm_pool_refill fingerprint_hash=" 90 >/dev/null
  wait_log_contains "${CONTAINER_NAME}" "warm_pool_state present=true" 30 >/dev/null

  echo "[smoke] Warm browser scrape (expect warm_hit=True)"
  local warm_response warm_body warm_code warm_logs
  warm_response="$(http_post "/scrape" '{"url":"https://example.com","execution_mode":"browser","navigation_mode":"get","max_retries":0,"headless":true}')"
  warm_body="$(echo "$warm_response" | sed '$d')"
  warm_code="$(echo "$warm_response" | tail -n1)"
  [[ "$warm_code" == "200" ]] || fail "Expected warm browser scrape 200, got ${warm_code}"
  assert_html_contains "$warm_body" "Example Domain" || fail "Warm scrape html missing marker"
  warm_logs="$(docker logs "${CONTAINER_NAME}" 2>&1)"
  echo "$warm_logs" | grep -E "scrape_boot .*warm_hit=True" >/dev/null \
    || fail "Expected scrape_boot warm_hit=True after warm handoff"

  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  echo "[smoke] Profile prewarm-on-warm-handoff PASS"
}

require_cmd docker
require_cmd curl
require_cmd python3

trap cleanup EXIT

cleanup
ensure_image

case "${SMOKE_PROFILE}" in
  contract-prewarm-off)
    run_contract_prewarm_off
    ;;
  prewarm-on-warm-handoff)
    run_prewarm_on_warm_handoff
    ;;
  all)
    run_contract_prewarm_off
    run_prewarm_on_warm_handoff
    ;;
  *)
    fail "Unknown SMOKE_PROFILE=${SMOKE_PROFILE} (expected contract-prewarm-off|prewarm-on-warm-handoff|all)"
    ;;
esac

echo "[smoke] PASS"
