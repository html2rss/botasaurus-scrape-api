# Typing residuals (strict mode)

Pyright runs in **`strict`** mode for `app/` and `tests/`. Baseline before this pass: **553** errors with no relaxations; gate target: **≤28** documented residuals with **`make check` exit 0**.

## Current gate

| Scope | Errors |
| --- | ---: |
| `app/` | **0** |
| `tests/` | **0** |
| **Total** | **0** |

## Test-module file directives

Most test modules pass strict pyright with no relaxation. The remaining directives are scoped per module:

| Module | Directive | Why |
| --- | --- | --- |
| `tests/api/test_request_schema.py` | full blanket line | dozens of raw-dict payload permutations |
| `tests/api/test_http_contract.py` | full blanket line | walks untyped `app.openapi()` dict |
| `tests/infra/test_xhr_collector.py` | full blanket line | drives protected handlers on dynamic CDP doubles |
| `tests/engine/test_timeout_progress.py` | full blanket line | `__getattr__`-based phase-probe driver |
| `tests/infra/test_sentry.py` | `reportPrivateUsage=false` | asserts module-private init state |
| `tests/engine/test_scraper_engine.py` | `reportPrivateUsage=false` | asserts `_active_request_ids` bookkeeping |

The full blanket line is the canonical eleven-rule directive:

```python
# pyright: reportMissingParameterType=false, reportUnknownParameterType=false, ...
```

**Rationale:** keeps strict checking on real argument/type errors (`reportArgumentType`, `reportReturnType`) while avoiding false positives on nested test doubles. Counted as **one policy per test module**, not per-error suppressions. Do not add the blanket line to new test modules that pass without it.

## Intentional boundary casts (app)

| Location | Rule avoided | Rationale |
| --- | --- | --- |
| `app/api/errors.py` | FastAPI handler registration | `RequestValidationError` handler registration uses `cast(Any, …)` because Starlette's `ExceptionHandler` union does not narrow on exception type. |
| `app/infra/metadata.py` | `getattr` on driver requests | Passive metadata reads duck-typed Botasaurus request objects; `getattr` + `list[object]` iteration at the vendor seam. |
| `tests/support/http.py` | `_EngineExecuteProxy` return | Dependency override injects a execute-only proxy; cast to `ScraperEngine` preserves FastAPI DI signature. |

## Vendor ownership

| Type surface | Owner |
| --- | --- |
| Botasaurus / CDP | `typings/` local `.pyi` stubs |
| Wire / domain models | `app/schemas/` |
| CDP log / pending XHR shapes | `app/infra/cdp_types.py` |
| Test `HttpUrl` construction | `tests/support/factories.py` (`scrape_request`, `example_url`) |

## Policy

- Do not add blanket `# type: ignore` in `app/`.
- New test code: prefer `scrape_request()` over raw `ScrapeRequest(url="…")`.
- If strict errors rise above **28**, fix or document before merging; do not reintroduce global test `executionEnvironments` relaxations.
