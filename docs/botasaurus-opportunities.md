# Botasaurus opportunities inventory

Goal: fewer `challenge_block` results (DataDome especially) without a residential proxy.

Sources: omkar.cloud/botasaurus (thin, v3-era; only names Cloudflare/PerimeterX), the
`omkarcloud/botasaurus` README, and the vendored driver at pinned commit `7936afad`
(`.venv/.../botasaurus_driver/driver.py`). Vendored code is ground truth where the docs are silent.
Nothing below is measured yet; "Expected" is a hypothesis to validate with `scripts/bench_scrape.py`
against real blocked hosts.

## A. Bugs / dead code found (fix first, cheap)

| # | Finding | Evidence | Fix |
| --- | --- | --- | --- |
| A1 | `settle_page_state` calls `bypass_cloudflare`; driver has no such method → **silent no-op**. The post-challenge "bypass" step never runs. | `browser_tier.py:86`; driver only has `detect_and_bypass_cloudflare` (`driver.py:1396`) | Call `detect_and_bypass_cloudflare`; only when marker is Cloudflare. |
| A2 | Detector driver signals `is_in_challenge`, `is_blocked` don't exist; `is_bot_detected` exists but only detects Cloudflare title/turnstile and PerimeterX text. **DataDome is invisible to driver checks.** | `detector.py:29-33`; `driver.py:1353-1371` | Drop the two dead names; use `get_bot_detected_by()` (returns vendor) as the marker label. |
| A3 | `sleep_random` doesn't exist → readiness/scroll always fall back to fixed `sleep(1)` / `sleep(0.5)` (robotic timing). | `strategies.py:145,181`; driver has `short_random_sleep` / `long_random_sleep` | Use those. |
| A4 | `organic_get` doesn't exist; `NavigationMode.ORGANIC_GET` silently degrades to `google_get`. | `strategies.py:68-69` | Either implement via `get_via(url, referer)` or remove the enum value (no-compat rule). |
| A5 | Bare `"datadome"` substring marker matches any page that merely embeds the DataDome JS tag/cookie name, so clean pages on DataDome-protected sites can be misreported as `challenge_block`. Real interstitial is `captcha-delivery.com` / `geo.captcha-delivery.com`. | `detector.py:19`; `clean.html` has 0 hits so the corpus wouldn't catch it | Add a clean-page-with-DataDome-tag fixture; narrow the marker; add a real-interstitial size/shape check (tiny body, `dd=` JS object). |

## B. Features the docs offer that we don't use

Ranked by expected impact on DataDome without residential proxy.

| # | Opportunity | Docs reference | Current state | Expected | Cost / risk |
| --- | --- | --- | --- | --- | --- |
| B1 | **Human mode**: `driver.enable_human_mode()` (humancursor mouse movement) before scroll/readiness. DataDome scores behavioral telemetry. | README "Human-like behavior" | unused | High | Low; slower by ~1-3s; add as request flag + auto-on for retry attempts. |
| B2 | **Never `headless=True` on protected hosts.** Docs: Cloudflare and DataDome "will surely" identify headless. Default is already headed+Xvfb. | README headless note | `headless` is a free request field | Medium (prevents footguns) | Log warning or ignore flag when a challenge marker was seen. |
| B3 | **Consistent fingerprint**: `user_agent=UserAgent.HASHED`, `window_size=WindowSize.HASHED` (stable per host/profile), instead of Botasaurus defaults. | README "Fingerprints" | passes only caller-supplied values | Medium | Low; key hash on host. UA/Chrome-version mismatch with real Chromium is itself a signal, so test before enabling. |
| B4 | **Referrer variety**: `get_via(url, referer=…)` / `get_via_this_page`. `google_get` uses one fixed Google referer. | README navigation | `google_get`, `google_get_bypass`, `get` | Low-Medium | Low. |
| B5 | **Retry ladder is wasteful**: every attempt reuses the same dirty browser, IP, and profile. After a DataDome 403 the next attempt sees the same cookie/session. Retry on a **fresh profile + new fingerprint** (needs a new driver per attempt). | README `max_retry` + profile docs | strategies loop inside one driver | Medium | Medium; budget (boot ~seconds) and `WorkLease` interplay. |
| B6 | **Carry earned cookies request-tier → browser and back.** Browser solves JS challenge → `driver.get_cookies_dict()` (includes `datadome`) → reuse in request tier for the same host+proxy+UA. DataDome cookies are bound to IP/UA. | `get_cookies_dict`, `add_cookies` | no cross-request reuse by contract | High for repeat feeds | **Violates "no cache/profile reuse across requests" contract**; needs explicit decision, TTL, per-host+proxy+UA key, README/docs update. |
| B7 | **Server-side proxy config + rotation**: `proxy=[list]` rotates in the decorator API. We only accept a per-request `proxy`. Add `SCRAPE_PROXY_URLS` in `Settings`, round-robin, and **escalate to proxy only on `challenge_block`** (README: "use proxies only when IP blocks occur"). | README proxies | proxy only if caller passes it | High *when a proxy exists*; zero otherwise | Settings + SSRF-guard for proxy host; fingerprint/warm-pool key already includes proxy. Gives the user a seam so html2rss need not pass creds per call. |
| B8 | **Datacenter-IP honesty**: DataDome mostly scores IP reputation; B1-B5 raise the ceiling but won't beat a flagged ASN. Docs recommend residential (IPRoyal) and say block images/CSS to cut bandwidth, plus fetch later pages via `driver.requests.get`. | README residential tips | `block_images` default true | n/a | Expectation-setting; see D. |
| B9 | **Request tier tuning**: `botasaurus-requests` already sends browser-ordered headers/TLS and a google.com referer; docs say requests need `max_retry` because connection challenges are probabilistic. We do 1 attempt then escalate. Add 1-2 cheap retries on 403 from Cloudflare "connection" style blocks before paying for Chromium. | README Cloudflare section | `attempts=1` | Low-Medium | Cheap; honor 429 with ~1.13s sleep per docs. |
| B10 | **Cloudflare Turnstile helpers**: `get_element_at_point`, iframe selection for widget solve. Already covered inside `detect_and_bypass_cloudflare` once A1 is fixed. | README | n/a | n/a | Fix A1 instead. |
| B11 | **CAPTCHA solver**: Capsolver extension (`extensions=[Capsolver(api_key=…)]`). Handles DataDome slider/captcha interstitials that need a human. | README | none | High on captcha-gated hosts | Paid third-party, new dependency and secret; opt-in via `Settings` only. |
| B12 | `tiny_profile=True` for lightweight cookie persistence, `delete_cookies()` on HTTP 400 "cookie too large". | README profiles/FAQ | `tiny_profile=False`, fresh dir | Low | Only relevant if B6 is accepted. |

## C. Diagnostics (to know what is actually failing before tuning)

1. Record per-attempt `marker`, `status`, `execution_tier`, and vendor (`get_bot_detected_by()` / matched marker) in `scrape_challenge_detected` events (already partly logged) and add **response header sniffing** in the request tier: `x-datadome`, `x-dd-b`, `set-cookie: datadome=`, `cf-mitigated`. Cheaper and more reliable than HTML substrings.
2. Add a per-host block-rate counter in `ops_telemetry` so B7 escalation and any host hint have data.
3. Bench protocol: pick 5 DataDome + 5 Cloudflare hosts; run N=20 each in `execution_mode=browser`, compare block rate before/after each of A1-A5, B1, B3. Record in PR body per AGENTS.md.

## D. Realistic ceiling without a proxy

Fixing A1-A5 and enabling B1/B3/B5 removes self-inflicted failures and may lift success on
fingerprint/behavior-scored sites. If the egress IP is a datacenter ASN already flagged by DataDome,
none of this will pass reliably; B7 (optional proxy, only on block) is the only lever that changes the
IP signal. Treat B6 (cookie reuse) as the largest non-proxy win but a deliberate contract change.

## E. Suggested order

1. A1-A5 (one PR: `fix(engine)`), with fixture for A5.
2. C1 header sniffing + C3 bench baseline.
3. B1 + B2 + B3 (one PR), measured.
4. B7 (`Settings` proxy pool, escalate on block).
5. Decide B6 / B5 / B11 with the user (contract and cost implications).
