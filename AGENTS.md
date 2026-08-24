# AGENTS.md

## Core Rules

- Docker-first and Docker-only unless user asks otherwise.
- Keep repo focused: stable Botasaurus scrape API wrapper, not generic framework.

## Contract (Do Not Break)

- Endpoints: `GET /health`, `POST /scrape`.
- `openapi.yaml` is generated from `app.openapi()` via `make openapi`. Do not hand-edit. `make openapi-verify` is part of `make check`. Spectral (`make spectral`) lints the snapshot; do not add a post-processor that mutates the dump.
- Wire types live in `app/schemas.py`. Engine imports them. Routes `model_dump()` once into `JSONResponse`.
- OpenAPI `info.version` is `2.0.0`. Schema names: `ScrapeSuccess` (200) and `ScrapeError` (400/403/422/502/504). No `ScrapeResponse` alias.
- Success `/scrape` fields: `url`, `final_url`, `status_code`, `headers`, `html`, `metadata_error`, `xhr_responses`, `diagnostics`.
- When `html` is present, document `headers` `content-type` is `text/html; charset=utf-8` and `html` is UTF-8-normalized.
- Error `/scrape` fields: `url`, `error`, `error_category`, `diagnostics`. No `html`.
- `diagnostics`: `request_id`, `attempts`, `strategy_used`, `render_ms`, `execution_tier`, `challenge` (`blocked`, `detected`, `marker`), optional `timeout_phase` (`queue` | `boot` | `work`) on timeout outcomes.
- Request options: `execution_mode`, `navigation_mode`, `max_retries`, `wait_for_selector`, `wait_timeout_seconds`, `scroll`, `block_images`, `block_images_and_css`, `block_trackers`, `wait_for_complete_page_load`, `headers`, `cookies`, `user_agent`, `window_size` (`{width, height}`), `lang`, `headless`, `proxy`. `scroll` means scroll-to-bottom / lazy-load. No `scroll_to_bottom`.
- `wait_timeout_seconds` outside `[1, SCRAPE_WORK_TIMEOUT_SECONDS]` (default 30) is clamped into that range so `/scrape` still runs; remaining schema 422 bodies use the scrape error envelope (`url`, `error`, `error_category`, `diagnostics`), not FastAPI `detail`. Do not advertise `minimum`/`maximum` as 422.
- `error_category`: `timeout`, `challenge_block`, `navigation_error`, `metadata_error`, `validation`. 400/422 use `validation`.
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
- Before each scrape, prune orphaned runtime dirs not tied to an active request id; ENOSPC on profile creation retries after another prune pass. Optional `SCRAPE_RUNTIME_MIN_FREE_BYTES` (default 256MiB) logs when the runtime filesystem is low.
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
- When Pydantic models or route response metadata change, run `make openapi` and commit the snapshot with the code change.
- When API contract, Docker behavior, or error semantics change, also run `make smoke`.
- `make smoke` must cover build, boot, `/health`, `/scrape` happy path, strategy override, retry path, isolation check, localhost guardrail.
- If API contract, Docker behavior, or error semantics changed, update README in same change.
- Keep commits scoped (infra vs API vs docs).
