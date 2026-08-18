# AGENTS.md

## Core Rules

- Docker-first and Docker-only unless user asks otherwise.
- Keep repo focused: stable Botasaurus scrape API wrapper, not generic framework.

## Contract (Do Not Break)

- Endpoints: `GET /health`, `POST /scrape`.
- Stable legacy `/scrape` fields: `url`, `final_url`, `status_code`, `headers`, `html`, `error`, `metadata_error`.
- Additive diagnostics fields (current contract): `request_id`, `attempts`, `strategy_used`, `render_ms`, `blocked_detected`, `challenge_detected`, `error_category`, `execution_tier`, `detected_challenge`, `xhr_responses`.
- Request options (current contract): `execution_mode`, `navigation_mode`, `max_retries`, `wait_for_selector`, `wait_timeout_seconds`, `scroll`, `scroll_to_bottom`, `block_images`, `block_images_and_css`, `block_trackers`, `wait_for_complete_page_load`, `headers`, `cookies`, `user_agent`, `window_size`, `lang`, `headless`, `proxy`.
- `wait_timeout_seconds` outside `[1, SCRAPE_TIMEOUT_SECONDS]` (default 20) is clamped into that range so `/scrape` still runs; remaining schema 422 bodies use the scrape envelope (`url`, `error`, `error_category`, `request_id`), not FastAPI `detail`.
- Error codes:
  - `400` validation/resolution failure
  - `403` SSRF guardrail block
  - `422` request schema validation
  - `502` scrape execution failure
  - `504` timeout

## Runtime + Browser Constraints

- `POST /scrape` is async API over sync browser work (threadpool).
- Each scrape request must use isolated runtime state:
  - request-scoped runtime dir `/tmp/scrape/<request_id>`
  - request-scoped browser profile
  - no cache/profile/driver reuse across requests
- Cleanup is mandatory in `finally`:
  - close browser driver
  - delete request runtime dir
  - remove in-memory active request id
- Keep request-id collision/invariant guard (`_active_request_ids`) intact.
- `driver.requests.get` metadata is best-effort; metadata failure must not fail HTML success.
- Keep strategy engine behavior:
  - `auto` mode attempt order: `google_get` -> `google_get_bypass` -> `get`
  - do not alter retry semantics without docs/tests update
- Multi-arch image required:
  - all architectures: Chromium install
  - keep `/usr/bin/google-chrome` symlink to Chromium for compatibility
- If browser install logic changes, re-verify binary path and Botasaurus startup.

## Safety

- Keep SSRF guardrails: localhost/domain checks and blocked IP classes (loopback/private/link-local/multicast/reserved/unspecified).
- Do not weaken URL validation without explicit request plus docs/tests updates.

## Done Criteria

- Run `make check` before finish.
- When API contract, Docker behavior, or error semantics change, also run `make smoke`.
- `make smoke` must cover build, boot, `/health`, `/scrape` happy path, strategy override, retry path, isolation check, localhost guardrail.
- If API contract, Docker behavior, or error semantics changed, update README in same change.
- Keep commits scoped (infra vs API vs docs).
