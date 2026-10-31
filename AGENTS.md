# AGENTS.md

## Core Rules

- Docker-first and Docker-only unless user asks otherwise.
- Stable Botasaurus scrape API wrapper — not a generic framework.
- Reduce LOCs; time matters; modern Python 3.15 (`StrEnum`, `type` aliases, `X | Y`, `pathlib.Path`, Pydantic v2, strict `pyright`). No free-threading, no experimental JIT.
- No backward compatibility; no shims; no long-lived re-export layers.

## Layers

| Layer | May import | Must not import |
| --- | --- | --- |
| `api/routes` | `domain`, `api/deps`, `schemas.*` | `engine` internals, Botasaurus |
| `domain` | `engine`, `security`, `schemas.*`, `infra` | FastAPI, Botasaurus |
| `engine` | `infra`, `security`, `schemas.*`, `config` | FastAPI |
| `infra` | Botasaurus, CDP (lazy at use sites) | FastAPI, routes |

Challenge corpus: `tests/fixtures/challenge/` is the single interstitial fixture home. `ChallengeDetector` markers and gem `BlockedSurface` must both assert those files.

## Conventions

- HTTP tests: `tests/support/http.test_client()` only (lifespan + `dependency_overrides`). No ad-hoc `TestClient(create_app())`.
- Loggers: `app.logging_config.get_logger()` only.
- Wall-clock math: `app/engine/budget.py` only; tiers must not re-derive.
- Env: add to `Settings` in `config.py`; tests that patch env call `reset_settings_cache()`.
- Wire types: `app/schemas/` direct imports (`from app.schemas.request import ScrapeRequest`).
- Domain logic out of routes and Pydantic shells. Typed exceptions (`RequestIdCollisionError`), not message matching.
- `NavigationMode` end-to-end; no raw strategy strings outside enum boundaries.
- Driver calls are direct and typed via `driver_protocol` (`DriverProtocol`/`TabProtocol`); no string-based `getattr` lookups. Declare only members the real `Driver`/`Tab` have — `tests/engine/test_driver_contract.py` enforces it. Best-effort close: `close_driver`.
- OpenAPI examples: `openapi_examples.py` model instances. `create_router()` included from `create_app()` **after** `configure_openapi(settings)`.
- Botasaurus/CDP imports stay function-local inside `run_request_tier`, `create_driver_from_fingerprint`, `configure_driver` (extra headers), and `XhrCollector` so tests can patch the vendor module. Do not use `lazy` or `__lazy_modules__` for those.
- One `ScraperEngine` + one `ThreadPoolExecutor` on `app.state` from lifespan; deps read `request.app.state`. Shutdown: `executor.shutdown(wait=False, cancel_futures=True)`.
- Do not freeze settings at import. Hot path injects `Settings`; no `get_settings()` fallback. `clamp_wait_timeout_seconds` reads live `get_settings()`.
- Types: `make typecheck` = `pyright` strict on `app tests`. Stubs in `typings/`; CDP in `app/infra/cdp_types.py`; seams `DriverProtocol` / `CdpTabProtocol`. Tests: `tests/support/factories.py`, `tests/support/fakes.py`. Prefer fixing types over `# pyright:` escapes.

## Isolation + Runtime

| Resource | Lifetime | Rule |
| --- | --- | --- |
| `ScraperEngine` / `ThreadPoolExecutor` | process (`app.state`) | shared; workers = `SCRAPE_MAX_WORKERS` |
| `HostConcurrencyGate` | process (`ScrapeService`) | per-host; `SCRAPE_MAX_PER_HOST` |
| `WorkLease` | per request | admit → executor → deadline reclaim (`force_close`); phase snapshot; `Future.cancel` ≠ Chromium reclaim |
| `WarmDriverPool` (opt-in) | process | single spare; refill on dedicated daemon — never scrape executor |
| `_active_request_ids` | in-process | collision → `RequestIdCollisionError` |
| `/tmp/scrape/<request_id>` + profile | per request | isolated; deleted in `finally`; warm `spare-*` dies with adopter |
| Botasaurus Driver | ≤1 request | closed in session `__exit__` / lease reclaim; never returned to pool |

- `WorkLease` owns outer deadline, host admit, phase snapshot, Chromium reclaim. `WorkLease.timeout_error` is the only outer-timeout envelope. Session exit + orphan prune are safety nets, not a second reclaim story.
- Default single-worker uvicorn (multi-worker breaks in-process collision unless sticky request ids).
- `POST /scrape`: async API over sync threadpool work. No cache/profile/driver reuse across requests.
- Prewarm (`SCRAPE_PREWARM=true`, default off): fingerprint-matched spare after browser scrape; idle TTL `SCRAPE_PREWARM_IDLE_TTL_SECONDS` (default `600`, `0`=never); min refill `SCRAPE_PREWARM_MIN_REFILL_SECONDS` (default `30`). Worst case 2 Chromiums briefly. Cgroup-v2 skip above 70% memory. Headed concurrent drivers OK (`PREWARM_HEADLESS_ONLY=False`).
- `finally`: close driver; delete runtime dir (+ adopted spare); scrub active request id. Before each scrape prune orphans; `live_spare_dirs()` protects ready/in-build/adopted `spare-*` (`release_adopted` on session exit). Orphan `spare-*` cleaned at pool init. ENOSPC → prune-and-retry. `SCRAPE_RUNTIME_MIN_FREE_BYTES` (default 256MiB) logs low free space.
- Metadata (`driver.requests.get`) is best-effort; failure must not fail HTML success.
- `auto` order: `google_get` → `google_get_bypass` → `get`. Do not alter retry semantics without docs/tests.
- Browser tier: skip XHR harvest before Cloudflare bypass; one `collect_page_state` after bypass.
- Multi-arch: Chromium on all arches; keep `/usr/bin/google-chrome` → Chromium symlink. Re-verify binary path + Botasaurus startup if install logic changes.

## Contract (Do Not Break)

- Endpoints: `GET /health`, `POST /scrape`. Field shapes: `app/schemas/` + `openapi.yaml` (`make openapi` / `make openapi-verify` / `make spectral`). Do not hand-edit or post-process the dump.
- OpenAPI `info.version` `2.0.0`. Schemas: `ScrapeSuccess` (200), `ScrapeError` (400/403/422/502/504). No `ScrapeResponse`. No `scroll_to_bottom` (`scroll` = scroll-to-bottom / lazy-load).
- Routes: `ScrapeService.process()` → `json_response()`.
- When `html` present: `headers` `content-type` is `text/html; charset=utf-8`; `html` is UTF-8-normalized. Errors have no `html`.
- `wait_timeout_seconds` outside `[1, SCRAPE_WORK_TIMEOUT_SECONDS]` (default 30) is clamped (scrape still runs). Remaining 422 bodies use scrape error envelope (`url`, `error`, `error_category`, `diagnostics`), not FastAPI `detail`. Do not advertise `minimum`/`maximum` as 422.
- `error_category`: `timeout` | `challenge_block` | `navigation_error` | `metadata_error` | `validation` (400/422). Codes: `400` validation/resolution, `403` SSRF, `422` schema, `502` execution, `504` timeout. Optional `diagnostics.timeout_phase`: `queue` | `boot` | `work`.

## Safety

- SSRF: localhost/domain checks + blocked IP classes (loopback/private/link-local/multicast/reserved/unspecified) in `app/security/`. Do not weaken without explicit request + docs/tests.

## Performance

- Bench: `PYTHONPATH=. .venv/bin/python3 scripts/bench_scrape.py --runs 10` (`execution_mode=request`). Record p50 wall time on hot-path changes.
- Cold vs warm: `SCRAPE_PREWARM=true` + `--execution-mode browser` (Docker `--shm-size=1gb --init`); compare `boot_ms` / p50; record delta in prewarm PR body.
- Low-RAM (`--memory=768m`, 1–2 vCPU): `SCRAPE_MAX_WORKERS` `1` or `2`. Keep prewarm default-off until canary shows no RSS ceiling breach.

## Done Criteria

- `make check` before finish (lint, test, typecheck, openapi-verify).
- Pydantic/route response metadata change → `make openapi` + commit snapshot.
- API/Docker/error-semantics change → `make smoke` + update README same change.
- `make smoke` (`SMOKE_PROFILE=all`): `contract-prewarm-off` (build/boot, health, scrape paths, isolation, SSRF, request-tier, headers, `organic_get`, scroll, Sentry sidecar); `prewarm-on-warm-handoff` (`SCRAPE_PREWARM=true`, cold then warm `warm_hit`). CI titles: `Smoke (prewarm=false, cold)`, `Smoke (prewarm=true, warm-handoff)`. Image job: `docker-smoke-image` (`scope=smoke-linux-amd64`); matrix: `cache-from` + `SMOKE_SKIP_BUILD=1`.
- Commits scoped (infra vs API vs docs).
