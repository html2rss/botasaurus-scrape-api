# Typing residuals (strict mode)

Pyright runs in **`strict`** mode for `app/` and `tests/`. Baseline before this pass: **553** errors with no relaxations; gate target: **≤28** documented residuals with **`make check` exit 0**.

## Current gate

| Scope | Errors |
| --- | ---: |
| `app/` | **0** |
| `tests/` | **0** |
| **Total** | **0** |

## Test-module file directives

Unittest modules under `tests/` (not `tests/support/`) carry a file-level directive relaxing rules that fight dynamic mocks and `TestClient` ergonomics without weakening production types:

```python
# pyright: reportMissingParameterType=false, reportUnknownParameterType=false, ...
```

**Rationale:** keeps strict checking on real argument/type errors (`reportArgumentType`, `reportReturnType`) while avoiding hundreds of false positives on nested test doubles. Counted as **one policy per test module**, not per-error suppressions.

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
