# Botasaurus Docker API

Docker-only FastAPI service that uses [Botasaurus](https://github.com/omkarcloud/botasaurus) to fetch rendered HTML and best-effort response metadata.

## What This Is

- Containerized API surface:
  - `GET /health`
  - `POST /scrape`
- Intended usage: run and test through Docker only.
- Runtime boundary: async FastAPI handler delegates sync browser work to a bounded threadpool (`SCRAPE_MAX_WORKERS`, default `4`), with per-request total and post-boot work timeouts (`SCRAPE_TIMEOUT_SECONDS`, default `45`; `SCRAPE_WORK_TIMEOUT_SECONDS`, default `30`).
- On-demand isolation-first runtime: every scrape request runs with an ephemeral browser profile and request-scoped runtime dir, then gets fully cleaned up.
- Optional Sentry ops telemetry when `SENTRY_DSN` is set (see [Sentry](#sentry-optional)).

## Prerequisites

- Docker
- `curl`
- `python3` (used by smoke assertions)

## Quick Start (Docker Only)

Run directly with Docker:

```bash
docker run --rm -p 4010:4010 html2rss/botasaurus-scrape-api:latest
```

Or run from this repository directory:

```bash
make serve
```

Health check:

```bash
make health
```

Example scrape:

```bash
make scrape-example
```

## Docker Compose

Use a dedicated Sentry project — map `BOTASAURUS_SENTRY_DSN` to `SENTRY_DSN`; do not reuse the html2rss-web DSN.

```yaml
services:
  botasaurus:
    image: html2rss/botasaurus-scrape-api:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:4010:4010"
    environment:
      SENTRY_DSN: ${BOTASAURUS_SENTRY_DSN:-}
      SENTRY_ENVIRONMENT: ${ENVIRONMENT:-production}
      SENTRY_RELEASE: ${GIT_SHA:-unknown}
```

## Published Image

Docker Hub image:

`html2rss/botasaurus-scrape-api`

Pull latest:

```bash
docker pull html2rss/botasaurus-scrape-api:latest
```

Pull immutable commit tag:

```bash
docker pull html2rss/botasaurus-scrape-api:<git-sha>
```

Publish policy:

- GitHub Actions publishes from `main` branch pushes.
- Published tags are `latest` and the full commit SHA.

## Smoke Test

Run end-to-end smoke checks (build, boot, health, scrape happy path, localhost guardrail, diagnostics, isolation):

```bash
make smoke
```

Expected result: script prints `[smoke] PASS` and exits `0`.

## API Contract

Machine-readable contract: [`openapi.yaml`](openapi.yaml), generated from the FastAPI app with `make openapi`. Do not hand-edit it. `make check` runs `make openapi-verify` and fails if the snapshot does not match a fresh `app.openapi()` dump.

A running container also serves FastAPI's live schema and UIs at `/openapi.json`, `/docs`, and `/redoc`. Those are framework defaults, not additional scrape endpoints.

Human examples follow. OpenAPI 2.0.0 is a breaking wire cut from 1.x:

- `scroll_to_bottom` → `scroll` (scroll means scroll-to-bottom / lazy-load)
- `window_size: [w, h]` → `{ "width": w, "height": h }`
- `blocked_detected` / `challenge_detected` / `detected_challenge` → `diagnostics.challenge.{blocked,detected,marker}`
- `request_id`, `attempts`, `strategy_used`, `render_ms`, `execution_tier`, optional `timeout_phase` → `diagnostics.*`
- error bodies no longer include `html`; 400/422 `error_category` is `validation`
- schema names `ScrapeResponse` → `ScrapeSuccess` + `ScrapeError`

### `GET /health`

Returns service status and detected Botasaurus version.

Example shape:

```json
{
  "status": "ok",
  "service": "botasaurus-scrape-api",
  "botasaurus_version": "4.x.x"
}
```

### `POST /scrape`

Optional transport header `X-Request-Id`: when present and path-safe, the same value is echoed in `diagnostics.request_id` on every response envelope (success and error).

Request body (minimum):

```json
{
  "url": "https://example.com"
}
```

Request body (full options):

```json
{
  "url": "https://example.com",
  "execution_mode": "auto",
  "navigation_mode": "auto",
  "max_retries": 2,
  "wait_for_selector": "h1",
  "wait_timeout_seconds": 15,
  "scroll": true,
  "block_images": true,
  "block_images_and_css": false,
  "block_trackers": true,
  "wait_for_complete_page_load": true,
  "user_agent": "Mozilla/5.0 ...",
  "headers": {
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "session=..."
  },
  "cookies": {
    "session": "..."
  },
  "window_size": {"width": 1920, "height": 1080},
  "lang": "en-US",
  "headless": false,
  "proxy": "http://user:pass@proxy:port"
}
```

Request options (contract):

- `execution_mode`:
  - `auto` (default): attempts fast anti-detect HTTP request (`curl_cffi` / TLS fingerprinting) first; escalates to real browser driver if challenge or dynamic hydration is required.
  - `request`: anti-detect HTTP request tier only (fastest, low memory).
  - `browser`: full headless/stealth Chromium browser tier.
- `navigation_mode`:
  - `auto` (default): `google_get` -> `google_get(bypass_cloudflare=true)` -> `get`
  - `get`: only `get`
  - `google_get`: only `google_get`
  - `google_get_bypass`: only `google_get(bypass_cloudflare=true)`
  - `organic_get`: only `organic_get`
- `max_retries`: `0..3`, default `2` (attempts = `1 + max_retries`, with `auto` capped by 3 strategy steps).
- `wait_for_selector`: if set, response waits for selector before capture (routes to browser tier).
- `wait_timeout_seconds`: selector wait timeout (default `15`). Values outside `[1, SCRAPE_WORK_TIMEOUT_SECONDS]` (default `30`) are clamped into that range so scrape still runs.
- `scroll`: if true, scrolls to the bottom to trigger lazy-loaded feeds (routes to browser tier).
- `block_images`: pass image blocking to driver. Default `true`.
- `block_images_and_css`: pass image+css blocking to driver. Default `false`.
- `block_trackers`: block tracking/ad networks and web fonts to speed up rendering. Default `true`.
- `wait_for_complete_page_load`: pass page-load wait behavior to driver. Default `true`.
- `user_agent`: explicit user agent string passed to driver.
- `headers`: custom HTTP request headers forwarded to request client or browser session.
- `cookies`: key-value cookies map forwarded to request client or browser session.
- `window_size`: viewport object `{ "width": 1920, "height": 1080 }` passed to driver.
- `lang`: browser language passed to driver (for example `en-US`).
- `headless`: pass headless browser mode to driver. Default `false`.
- `proxy`: proxy URL passed to driver. Invalid or blocked proxy URLs are rejected by SSRF guardrails.

Success response (`ScrapeSuccess`, HTTP 200):

```json
{
  "url": "https://example.com",
  "final_url": "https://example.com/",
  "status_code": 200,
  "headers": {
    "content-type": "text/html; charset=utf-8"
  },
  "html": "<!doctype html>...",
  "metadata_error": null,
  "xhr_responses": [],
  "diagnostics": {
    "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
    "attempts": 1,
    "strategy_used": null,
    "render_ms": 154,
    "execution_tier": "http_request",
    "challenge": {
      "blocked": false,
      "detected": false,
      "marker": null
    }
  }
}
```

Error response (`ScrapeError`, HTTP 400/403/422/502/504). No `html`:

```json
{
  "url": "https://example.com",
  "error": "Target URL is blocked",
  "error_category": "validation",
  "diagnostics": {
    "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
    "attempts": 0,
    "strategy_used": null,
    "render_ms": 0,
    "execution_tier": null,
    "challenge": null
  }
}
```

Field behavior:

- `html`: rendered page HTML, UTF-8-normalized. Present on success only.
- `headers`, `status_code`, `final_url`: best-effort metadata and may be `null`. When `html` is present, document `headers` `content-type` is `text/html; charset=utf-8`.
- `error`: failure message on `ScrapeError`.
- `metadata_error`: populated when metadata extraction fails but HTML scrape succeeds.
- `diagnostics.request_id`: unique per request for tracing.
- `diagnostics.attempts`: actual attempts performed.
- `diagnostics.strategy_used`: browser navigation strategy on the final attempt, or `null` on the HTTP-request tier.
- `diagnostics.render_ms`: elapsed render/runtime milliseconds.
- `diagnostics.execution_tier`: `http_request` or `browser_driver`.
- `diagnostics.challenge`: anti-bot assessment (`blocked`, `detected`, `marker`), or `null` when detection did not run.
- `diagnostics.timeout_phase`: on `error_category=timeout` only — which stage burned the budget: `queue` (threadpool wait), `boot` (browser/driver start), or `work` (navigate/wait/scroll). `null` on non-timeout outcomes.
- `xhr_responses`: always-on additive list of JSON XHR/fetch sub-resource bodies captured during the browser tier (empty for HTTP-request tier). Each entry is `{url, status_code, headers, body}`. `headers` keep only `content-type` (Set-Cookie and other headers are dropped). Caps: at most 20 responses, 500 KB per body, 2 MB aggregate across bodies. Main document responses are excluded. Collector state is reset between strategy retries so challenge interstitials do not pollute a later successful attempt. Candidate filtering for article-likeness is a client concern.
- `error_category`:
  - `timeout` — outer handler budget, queue wait, or a tier exception whose message contains `timeout` (browser and request tiers). Those tier timeouts also set `diagnostics.timeout_phase` (`boot`/`work`; request-tier timeouts are `work`).
  - `challenge_block`
  - `navigation_error`
  - `metadata_error`
  - `validation` (400 URL rejection and 422 schema failures)

Status codes:

- `200`: scrape completed (`ScrapeSuccess`).
- `400`: URL rejected by validation (for example unresolved host). `error_category` is `validation`.
- `403`: URL blocked by SSRF guardrails.
- `422`: request schema validation failed. Body is the scrape error envelope (`url`, `error`, `error_category`, `diagnostics`), not FastAPI `{"detail":[...]}`. `error_category` is `validation`.
- `502`: scrape execution failure/challenge block.
- `504`: scrape timed out.

## Runtime Flow And Invariants

`POST /scrape` executes this path:

1. Validate URL and SSRF guardrails.
2. Run scrape work in threadpool (`loop.run_in_executor`).
3. Build request-scoped runtime dir and profile under `/tmp/scrape/<request_id>`.
4. Create `Driver(...)` with request options.
5. Run strategy loop (`auto` or explicit mode) and optional selector wait.
6. Return HTML plus best-effort metadata (`driver.requests.get`).
7. Always run cleanup in `finally`:
   - close driver
   - delete runtime dir
   - remove request id from in-memory active set

Enforced invariants:

- No cache/profile/driver reuse across requests.
- Request id collision guard is enforced in memory before scrape starts.
- Metadata fetch failure does not discard successful HTML capture (`metadata_error` is set instead).

## URL Safety

The service accepts only `http` and `https` input URLs and blocks sensitive destinations before scrape execution.

Blocked targets include:

- `localhost` and `*.localhost`
- loopback addresses
- private network ranges
- link-local addresses
- multicast, reserved, and unspecified addresses

Exception:

- IPv6 NAT64 well-known prefix `64:ff9b::/96` is allowed.

## Isolation Guarantee

- Each `/scrape` request gets its own runtime directory: `/tmp/scrape/<request_id>`.
- Browser profile/session artifacts are request-scoped only.
- No cache/profile/driver reuse across requests.
- Cleanup is enforced in `finally`: driver close + runtime directory delete + request-id in-memory state scrub.

## Environment Variables

### Sentry (optional)

Use a **separate Sentry project** from html2rss-web (`BOTASAURUS_SENTRY_DSN` → `SENTRY_DSN`). Disabled when `SENTRY_DSN` is unset.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SENTRY_DSN` | _(unset)_ | Project DSN. |
| `SENTRY_ENVIRONMENT` | `production` | Deployment tag (`ENVIRONMENT` fallback). |
| `SENTRY_RELEASE` | _(unset)_ | Release tag on events. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | APM traces (off by default; enable later if needed). |
| `SENTRY_PROFILES_SAMPLE_RATE` | `0.0` | Profiling sample rate. |
| `SENTRY_SEND_DEFAULT_PII` | `false` | Send default PII when `true`. |

**Signal routing:** `navigation_error` and `timeout` → grouped Sentry Issues. `challenge_block` → `scrape.challenge_block` metric only; engine stdout keeps the detailed log line. Traces stay off unless you raise `SENTRY_TRACES_SAMPLE_RATE`.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SCRAPE_MAX_WORKERS` | `4` | Threadpool worker limit for sync browser execution. |
| `SCRAPE_TIMEOUT_SECONDS` | `45` | Handler wall-clock budget in seconds (queue, browser boot, and work). |
| `SCRAPE_WORK_TIMEOUT_SECONDS` | `30` | Post-boot navigate, selector wait, and scroll budget in seconds. |

## Example Calls


Easy mode:

```bash
curl -s -X POST http://localhost:4010/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'
```

Hard-target mode:

```bash
curl -s -X POST http://localhost:4010/scrape \
  -H 'Content-Type: application/json' \
  -d '{
    "url":"https://truthsocial.com/@realDonaldTrump",
    "navigation_mode":"auto",
    "max_retries":2,
    "wait_timeout_seconds":15,
    "headless":false
  }'
```

Challenge-target mode (recommended):

```bash
curl -s -X POST http://localhost:4010/scrape \
  -H 'Content-Type: application/json' \
  -d '{
    "url":"https://www.wsj.com/",
    "navigation_mode":"auto",
    "max_retries":2,
    "headless":false,
    "proxy":"http://user:pass@residential-proxy:port"
  }'
```

Note: if your IP is already flagged, you may still get challenge pages. In that case use a fresh residential IP.

## Make Targets

- `make` / `make check`: Ruff, Hadolint, Spectral, unit tests, and `make openapi-verify`.
- `make lint`: Ruff plus Hadolint plus Spectral.
- `make spectral`: lint `openapi.yaml` with Spectral in Docker (`stoplight/spectral:6`).
- `make ready`: same as `make check` (pre-PR gate; run `make smoke` when Docker behavior changes).
- `make test`: run host unit tests.
- `make openapi`: regenerate `openapi.yaml` from `app.openapi()`.
- `make openapi-verify`: fail if `openapi.yaml` does not match a fresh dump.
- `make build`: build Docker image.
- `make serve`: build and run API container on `localhost:4010`.
- `make health`: call `GET /health` on running service.
- `make scrape-example`: call `POST /scrape` with `https://example.com`.
- `make smoke`: run end-to-end smoke suite.
