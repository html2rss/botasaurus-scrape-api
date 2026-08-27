# AGENTS.md

## Core Rules

- Docker-first and Docker-only unless user asks otherwise.
- Keep repo focused: stable Botasaurus scrape API wrapper, not generic framework.

## Project Layout

```
app/
  main.py                    # create_app() factory; module-level `app` for uvicorn
  constants.py               # SERVICE_NAME and other shared literals
  config.py                  # Settings + nested SentrySettings; single env source of truth
  exceptions.py              # domain exceptions (e.g. RequestIdCollisionError)
  logging_config.py          # setup_logging + get_logger(); single owner of logger name
  api/
    deps.py                  # FastAPI Depends: settings, engine, executor, ScrapeService
    errors.py                # 422 + 500 handlers → scrape error envelope
    openapi.py               # route OpenAPI metadata (configure_openapi at app creation)
    openapi_examples.py      # OpenAPI examples built from Pydantic model instances
    routes/                  # thin HTTP handlers (health, scrape)
  domain/
    scrape_service.py        # request-id resolution, URL guardrails, threadpool execution, status mapping
  engine/
    orchestrator.py          # ScraperEngine.execute
    session.py               # ScrapeSession lifecycle
    budget.py                # wall-clock budget math shared across tiers (elapsed_ms, step budgets)
    request_tier.py          # HTTP/curl_cffi path
    browser_tier.py          # Chromium path
    warm_pool.py             # opt-in WarmDriverPool + DriverFingerprint
    strategies.py            # NavigationMode resolution, driver helpers
    driver_capabilities.py   # DriverProtocol + call_if_available adapter
    envelope.py              # success/error builders, UTF-8 HTML normalization
  schemas/
    enums.py                 # ExecutionMode, NavigationMode, ErrorCategory, ...
    request.py               # ScrapeRequest and validators
    response.py              # ScrapeSuccess, ScrapeError, HealthResponse, ...
  infra/                     # telemetry, progress, metadata, xhr, runtime cleanup, sentry
  security/                  # UrlGuard SSRF guardrails
scripts/
  bench_scrape.py            # TestClient wall-time bench for POST /scrape (request tier)
tests/
  api/                       # HTTP contract, request schema, 504 timeout envelope tests
  domain/                    # ScrapeService unit tests (timeout error mapping)
  engine/                    # ScraperEngine units, isolation regressions, timeout progress
  infra/                     # challenge, metadata, xhr, progress, sentry, telemetry, request-id, cleanup
  security/                  # UrlGuard tests
  support/
    http.py                  # test_client() context manager + dependency_overrides helper
    fakes.py                 # shared FakeDriver, FakeRequest, fake_request_cls, ...
    factories.py             # scrape_request(), example_url()
  test_bench_regression.py   # lightweight guard that bench script completes (root: guards scripts/)
```

Layer rules:

| Layer | May import | Must not import |
| --- | --- | --- |
| `api/routes` | `domain`, `api/deps`, `schemas.*` | `engine` internals, Botasaurus |
| `domain` | `engine`, `security`, `schemas.*`, `infra` | FastAPI, Botasaurus |
| `engine` | `infra`, `security`, `schemas.*`, `config` | FastAPI |
| `infra` | Botasaurus, CDP (lazy at use sites) | FastAPI, routes |

Conventions:

- Use `tests/support/http.test_client()` for HTTP tests; it runs lifespan and manages `dependency_overrides`. No ad-hoc `TestClient(create_app())` in test modules.
- Loggers come from `app.logging_config.get_logger()`; do not call `logging.getLogger` with a literal name.
- Wall-clock/timeout math (elapsed, remaining, step budgets) lives in `app/engine/budget.py`; tiers must not re-derive it.
- Config: add env vars to `Settings` in `config.py`; call `reset_settings_cache()` in tests that patch env.
- Wire types live in `app/schemas/` submodules; import directly (`from app.schemas.request import ScrapeRequest`). No long-lived re-export shim.
- Domain logic stays out of route handlers and Pydantic shells.
- Typed exceptions over string-matching (`RequestIdCollisionError`, not `RuntimeError` message checks).
- `NavigationMode` end-to-end in engine code; no raw strategy strings outside enum conversion boundaries.
- Optional Botasaurus driver methods go through `driver_capabilities.call_if_available` / `resolve_callable` only; do not ad-hoc `getattr(driver, ...)`.
- OpenAPI route examples come from `openapi_examples.py` model instances, not hand-typed dicts.
- Route modules expose `create_router()` factories included from `create_app()` **after** `configure_openapi(settings)`, so timeout-dependent response metadata is not frozen on first import.
- Botasaurus/CDP imports are lazy inside tier entrypoints (`run_request_tier`, `run_browser_tier`, XhrCollector methods), not at app import time.

## Singleton + Settings Threading

- **One** `ScraperEngine` and **one** `ThreadPoolExecutor` are created in `create_app()` lifespan and stored on `app.state`.
- `get_engine` / `get_executor` / `SettingsDep` read from `request.app.state` (not per-request construction).
- Lifespan shutdown calls `executor.shutdown(wait=False, cancel_futures=True)` on the real pool instance.
- Do **not** freeze settings at import time in schemas or OpenAPI modules. `configure_openapi(settings)` runs during `create_app()`; `clamp_wait_timeout_seconds` reads live `get_settings()`.
- `ScraperEngine` and tier functions require an injected `Settings` parameter; no `get_settings()` fallback in the hot path.

## Isolation Invariants

| Resource | Lifetime | Rule |
| --- | --- | --- |
| `ScraperEngine` | process (app.state) | shared |
| `ThreadPoolExecutor` | process (app.state) | shared, sized by `SCRAPE_MAX_WORKERS` |
| `WarmDriverPool` (opt-in) | process (engine.warm_pool) | single spare slot; refill on dedicated daemon thread — never the scrape executor |
| `_active_request_ids` | in-process memory | shared; collision guard |
| runtime dir `/tmp/scrape/<request_id>` | per request | isolated; deleted in `finally` |
| browser profile | per request (or adopted spare-*) | isolated; no reuse across requests; warm spare dies with the adopting request |
| Botasaurus Driver | may start before assignment | usage stays ≤1 request; closed in session `__exit__`; never returned to the pool |

Multi-worker uvicorn breaks in-process collision detection unless request ids are sticky to a worker. Default to single-worker for isolation semantics.

## Contract (Do Not Break)

- Endpoints: `GET /health`, `POST /scrape`.
- `openapi.yaml` is generated from `app.openapi()` via `make openapi`. Do not hand-edit. `make openapi-verify` is part of `make check`. Spectral (`make spectral`) lints the snapshot; do not add a post-processor that mutates the dump.
- Wire types live in `app/schemas/`. Engine imports them. Routes call `ScrapeService.process()` and serialize via `json_response()`.
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
  - request-scoped browser profile (cold path) or one-shot adopted `spare-*` profile (warm path)
  - no cache/profile/driver reuse across requests (warm spare is closed after the adopting request)
- Opt-in prewarm (`SCRAPE_PREWARM=true`, default off): single-slot `WarmDriverPool` builds a spare after a browser scrape finishes when the fingerprint matches. Refill runs on a dedicated daemon thread. Idle TTL (`SCRAPE_PREWARM_IDLE_TTL_SECONDS`, default `600`, `0`=never) and min refill interval (`SCRAPE_PREWARM_MIN_REFILL_SECONDS`, default `30`) bound idle RAM. Worst case during refill overlap: 2 Chromiums briefly. Cgroup-v2 best-effort skip above 70% memory. Docker Xvfb spike confirmed concurrent headed drivers are safe (`PREWARM_HEADLESS_ONLY=False`).
- Cleanup is mandatory in `finally`:
  - close browser driver
  - delete request runtime dir (and adopted spare dir when present)
  - remove in-memory active request id
- Before each scrape, prune orphaned runtime dirs not tied to an active request id; live warm spare dirs are protected. Orphan `spare-*` cleaned at pool init. ENOSPC on profile creation retries after another prune pass. Optional `SCRAPE_RUNTIME_MIN_FREE_BYTES` (default 256MiB) logs when the runtime filesystem is low.
- Keep request-id collision/invariant guard (`_active_request_ids`) intact; raises `RequestIdCollisionError`.
- `driver.requests.get` metadata is best-effort; metadata failure must not fail HTML success.
- Keep strategy engine behavior:
  - `auto` mode attempt order: `google_get` -> `google_get_bypass` -> `get`
  - do not alter retry semantics without docs/tests update
- Multi-arch image required:
  - all architectures: Chromium install
  - keep `/usr/bin/google-chrome` symlink to Chromium for compatibility
- If browser install logic changes, re-verify binary path and Botasaurus startup.

## Performance

- Baseline bench: `PYTHONPATH=. .venv/bin/python3 scripts/bench_scrape.py --runs 10` (TestClient, `execution_mode=request`).
- Browser cold vs warm: `SCRAPE_PREWARM=true` with `--execution-mode browser` (local Docker with `--shm-size=1gb --init` recommended); compare `boot_ms` from `scrape_boot` logs / p50 wall time. Record p50 `boot_ms` delta in the PR body when opening a prewarm PR.
- Record p50 wall time when changing hot paths; avoid regressions vs prior baseline.
- On low-RAM hosts (Docker `--memory=768m`, 1–2 vCPU), tune `SCRAPE_MAX_WORKERS` to `1` or `2`; higher values increase queue wait and swap without improving wall time. Keep prewarm default-off until canary shows no RSS ceiling breach.
- Browser tier skips XHR harvest before Cloudflare bypass; one consolidated `collect_page_state` pass runs after bypass.

## Types

- `make typecheck` runs **`pyright` strict** on `app tests` and is part of `make check`.
- Vendor seams: local stubs in `typings/` (`stubPath` in `pyproject.toml`); CDP shapes in `app/infra/cdp_types.py`.
- Engine driver seams use `DriverProtocol` / `CdpTabProtocol` in `driver_capabilities.py`; cast vendor `Driver` at construction when needed.
- Tests construct `ScrapeRequest` via `tests/support/factories.py` (`scrape_request`, `example_url`); fakes implement protocol shapes in `tests/support/fakes.py`.
- Strict pyright on `app/` and `tests/`; scoped `# pyright:` file directives only when a test module cannot pass strict without them (prefer fixing types or fakes first).

## Safety

- Keep SSRF guardrails: localhost/domain checks and blocked IP classes (loopback/private/link-local/multicast/reserved/unspecified).
- Do not weaken URL validation without explicit request plus docs/tests updates.

## Done Criteria

- Run `make check` before finish (lint, test, typecheck, openapi-verify).
- When Pydantic models or route response metadata change, run `make openapi` and commit the snapshot with the code change.
- When API contract, Docker behavior, or error semantics change, also run `make smoke`.
- `make smoke` must cover build, boot, `/health`, `/scrape` happy path, strategy override, retry path, isolation check, localhost guardrail.
- If API contract, Docker behavior, or error semantics changed, update README in same change.
- Keep commits scoped (infra vs API vs docs).
