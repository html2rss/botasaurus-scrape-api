# Botasaurus Docker API

Docker-only FastAPI service that uses [Botasaurus](https://github.com/omkarcloud/botasaurus) to fetch rendered HTML and best-effort response metadata.

## What This Is

- Containerized API surface:
  - `GET /health`
  - `POST /scrape`
- Intended usage: run and test through Docker only.
- Runtime boundary: async FastAPI handler delegates sync browser work to a bounded threadpool (`SCRAPE_MAX_WORKERS`), with a per-request timeout (`SCRAPE_TIMEOUT_SECONDS`).
- On-demand isolation-first runtime: every scrape request runs with an ephemeral browser profile and request-scoped runtime dir, then gets fully cleaned up.

## Prerequisites

- Docker
- `curl`
- `python3` (used by smoke assertions)

## Quick Start (Docker Only)

Run from this repository directory:

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
  "navigation_mode": "auto",
  "max_retries": 2,
  "wait_for_selector": "h1",
  "wait_timeout_seconds": 15,
  "block_images": true,
  "block_images_and_css": false,
  "wait_for_complete_page_load": true,
  "user_agent": "Mozilla/5.0 ...",
  "window_size": [1920, 1080],
  "lang": "en-US",
  "headless": false,
  "proxy": "http://user:pass@proxy:port"
}
```

Request options (contract):

- `navigation_mode`:
  - `auto` (default): `google_get` -> `google_get(bypass_cloudflare=true)` -> `get`
  - `get`: only `get`
  - `google_get`: only `google_get`
  - `google_get_bypass`: only `google_get(bypass_cloudflare=true)`
- `max_retries`: `0..3`, default `2` (attempts = `1 + max_retries`, with `auto` capped by 3 strategy steps).
- `wait_for_selector`: if set, response waits for selector before capture.
- `wait_timeout_seconds`: selector wait timeout (capped by service timeout).
- `block_images`: pass image blocking to driver. Default `true`.

Currently accepted passthrough options (implemented, not part of stable request-options contract):

- `block_images_and_css`: pass image+css blocking to driver.
- `wait_for_complete_page_load`: pass page-load wait behavior to driver.
- `user_agent`: explicit user agent string passed to driver.
- `window_size`: two-item integer list `[width, height]` passed to driver.
- `lang`: browser language passed to driver (for example `en-US`).
- `headless`: pass headless browser mode to driver. Default `false`.
- `proxy`: proxy URL passed to driver.

Success response shape (legacy fields preserved, additive diagnostics included):

```json
{
  "url": "https://example.com",
  "final_url": "https://example.com/",
  "status_code": 200,
  "headers": {
    "content-type": "text/html"
  },
  "html": "<!doctype html>...",
  "error": null,
  "metadata_error": null,
  "request_id": "b01ef2f8-f641-4e75-8ef2-0b73f7b4f372",
  "attempts": 1,
  "strategy_used": "google_get",
  "render_ms": 1268,
  "blocked_detected": false,
  "challenge_detected": false,
  "error_category": null
}
```

Field behavior:

- `html`: rendered page HTML.
- `headers`, `status_code`, `final_url`: best-effort metadata and may be `null`.
- `error`: populated when scrape fails or challenge is detected on final attempt.
- `metadata_error`: populated when metadata extraction fails but HTML scrape succeeds.
- `request_id`: unique per request for tracing.
- `attempts`: actual attempts performed.
- `strategy_used`: strategy used on final attempt.
- `render_ms`: elapsed render/runtime milliseconds.
- `blocked_detected` / `challenge_detected`: anti-bot signal flags from HTML/status markers.
- `error_category`:
  - `timeout`
  - `challenge_block`
  - `navigation_error`
  - `metadata_error`

Status codes:

- `200`: scrape completed without `error`.
- `400`: URL rejected by validation (for example unresolved host).
- `403`: URL blocked by SSRF guardrails.
- `422`: request schema validation failed.
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

Non-goals:

- No persistent login/session continuity across requests.
- No cross-request cookie sharing.

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
    "wait_timeout_seconds":20,
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

- `make build`: build Docker image.
- `make serve`: build and run API container on `localhost:4010`.
- `make health`: call `GET /health` on running service.
- `make scrape-example`: call `POST /scrape` with `https://example.com`.
- `make smoke`: run end-to-end smoke suite.
