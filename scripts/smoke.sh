#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${SMOKE_IMAGE_NAME:-botasaurus-api-smoke}"
CONTAINER_NAME="${SMOKE_CONTAINER_NAME:-botasaurus-api-smoke-run}"
HOST_PORT="${SMOKE_HOST_PORT:-4010}"
CONTAINER_PORT=4010
BASE_URL="http://127.0.0.1:${HOST_PORT}"

cleanup() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
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

key = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
value = obj.get(key)
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

key = sys.argv[1]
expected = sys.argv[2]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
actual = str(obj.get(key))
if actual != expected:
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
attempts = int(obj.get("attempts") or 0)
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

key = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
if obj.get(key) is not True:
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

key = sys.argv[1]
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
if obj.get(key) is not False:
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

key = sys.argv[1]
minimum = int(sys.argv[2])
raw = os.environ["JSON_INPUT"]
obj = json.loads(raw)
value = int(obj.get(key) or 0)
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

require_cmd docker
require_cmd curl
require_cmd python3

trap cleanup EXIT

cleanup

echo "[smoke] Building Docker image: ${IMAGE_NAME}"
docker build -t "${IMAGE_NAME}" .

echo "[smoke] Starting container: ${CONTAINER_NAME}"
docker run -d --name "${CONTAINER_NAME}" -p "${HOST_PORT}:${CONTAINER_PORT}" "${IMAGE_NAME}" >/dev/null

echo "[smoke] Waiting for /health"
for attempt in $(seq 1 30); do
  if response=$(curl -sS -w '\n%{http_code}' "${BASE_URL}/health" 2>/dev/null); then
    health_body="$(echo "$response" | sed '$d')"
    health_code="$(echo "$response" | tail -n1)"
    if [[ "$health_code" == "200" ]]; then
      break
    fi
  fi
  sleep 1
  if [[ "$attempt" == "30" ]]; then
    fail "Service did not become healthy in time"
  fi
done

assert_json_key_equals "$health_body" "status" "ok" || fail "Health status != ok"
assert_json_key_nonempty "$health_body" "service" || fail "Health service is empty"
assert_json_key_nonempty "$health_body" "botasaurus_version" || fail "Health botasaurus_version is empty"

echo "[smoke] Checking /scrape happy path"
scrape_response="$(http_post "/scrape" '{"url":"https://example.com"}')"
scrape_body="$(echo "$scrape_response" | sed '$d')"
scrape_code="$(echo "$scrape_response" | tail -n1)"
[[ "$scrape_code" == "200" ]] || fail "Expected /scrape happy path 200, got ${scrape_code}"
assert_json_key_nonempty "$scrape_body" "html" || fail "Scrape html is empty"
assert_html_contains "$scrape_body" "Example Domain" || fail "Scrape html missing expected marker"
assert_json_key_nonempty "$scrape_body" "request_id" || fail "Missing request_id"
assert_json_attempts_gte "$scrape_body" 1 || fail "Expected attempts >= 1"
assert_json_key_nonempty "$scrape_body" "strategy_used" || fail "Missing strategy_used"
assert_json_key_int_gte "$scrape_body" "render_ms" 1 || fail "Expected render_ms > 0"
assert_json_key_false "$scrape_body" "blocked_detected" || fail "Expected blocked_detected false"
assert_json_key_false "$scrape_body" "challenge_detected" || fail "Expected challenge_detected false"

echo "[smoke] Checking explicit strategy override"
strategy_response="$(http_post "/scrape" '{"url":"https://example.com","navigation_mode":"google_get_bypass","max_retries":0}')"
strategy_body="$(echo "$strategy_response" | sed '$d')"
strategy_code="$(echo "$strategy_response" | tail -n1)"
[[ "$strategy_code" == "200" ]] || fail "Expected explicit strategy scrape 200, got ${strategy_code}"
assert_json_key_equals "$strategy_body" "attempts" "1" || fail "Expected attempts=1 for max_retries=0"
assert_json_key_equals "$strategy_body" "strategy_used" "google_get_bypass" || fail "Expected strategy_used=google_get_bypass"

echo "[smoke] Checking retry path + final strategy in auto mode"
retry_response="$(http_post "/scrape" '{"url":"https://example.com","navigation_mode":"auto","max_retries":2,"wait_for_selector":"#definitely-missing-selector","wait_timeout_seconds":1}')"
retry_body="$(echo "$retry_response" | sed '$d')"
retry_code="$(echo "$retry_response" | tail -n1)"
[[ "$retry_code" == "502" ]] || fail "Expected retry failure 502, got ${retry_code}"
assert_json_key_equals "$retry_body" "attempts" "3" || fail "Expected auto mode to attempt 3 strategies"
assert_json_key_equals "$retry_body" "strategy_used" "get" || fail "Expected final strategy_used=get"
assert_json_key_equals "$retry_body" "error_category" "navigation_error" || fail "Expected navigation_error category"

echo "[smoke] Checking per-request isolation (no cookie leak)"
set_cookie_response="$(http_post "/scrape" '{"url":"https://httpbin.org/cookies/set?isotest=1","navigation_mode":"get","max_retries":0}')"
set_cookie_code="$(echo "$set_cookie_response" | tail -n1)"
[[ "$set_cookie_code" == "200" ]] || fail "Expected cookie set scrape 200, got ${set_cookie_code}"

check_cookie_response="$(http_post "/scrape" '{"url":"https://httpbin.org/cookies","navigation_mode":"get","max_retries":0}')"
check_cookie_body="$(echo "$check_cookie_response" | sed '$d')"
check_cookie_code="$(echo "$check_cookie_response" | tail -n1)"
[[ "$check_cookie_code" == "200" ]] || fail "Expected cookie check scrape 200, got ${check_cookie_code}"
assert_html_not_contains "$check_cookie_body" "isotest" || fail "Cookie leaked across requests"

echo "[smoke] Checking SSRF guardrail (/scrape localhost)"
blocked_response="$(http_post "/scrape" '{"url":"http://localhost"}')"
blocked_body="$(echo "$blocked_response" | sed '$d')"
blocked_code="$(echo "$blocked_response" | tail -n1)"
[[ "$blocked_code" == "403" ]] || fail "Expected localhost scrape 403, got ${blocked_code}"
assert_json_key_nonempty "$blocked_body" "error" || fail "Blocked response missing error message"

echo "[smoke] PASS"
