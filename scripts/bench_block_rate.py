#!/usr/bin/env python3
"""Interleaved block-rate A/B against a running service (``make serve``).

Variants alternate every round so egress-IP drift hits all of them equally.
Variants are JSON objects merged into the scrape payload, e.g.

    scripts/bench_block_rate.py https://example.com/ --runs 20 \
        --variant baseline='{"execution_mode":"browser"}' \
        --variant cookies='{"execution_mode":"browser","cookies":{"a":"b"}}'
"""

import argparse
import collections
import json
import statistics
import time
import urllib.error
import urllib.request


def scrape(base_url: str, body: dict[str, object], timeout: int) -> tuple[str, float]:
    request = urllib.request.Request(
        f"{base_url}/scrape",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        payload = json.load(exc)
    except OSError as exc:
        return f"client:{type(exc).__name__}", time.perf_counter() - started
    outcome = payload.get("error_category") or "ok"
    return outcome, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interleaved block-rate A/B")
    parser.add_argument("url")
    parser.add_argument("--base-url", default="http://localhost:4010")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=75)
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="NAME=JSON",
        help="payload overrides; default compares request vs browser",
    )
    args = parser.parse_args(argv)

    raw = args.variant or [
        'request={"execution_mode":"request"}',
        'browser={"execution_mode":"browser"}',
    ]
    variants: dict[str, dict[str, object]] = {}
    for item in raw:
        name, _, spec = item.partition("=")
        variants[name] = json.loads(spec or "{}")

    outcomes: dict[str, collections.Counter[str]] = {
        n: collections.Counter() for n in variants
    }
    timings: dict[str, list[float]] = {n: [] for n in variants}
    for _ in range(args.runs):
        for name, overrides in variants.items():
            outcome, seconds = scrape(
                args.base_url.rstrip("/"), {"url": args.url, **overrides}, args.timeout
            )
            outcomes[name][outcome] += 1
            timings[name].append(seconds)

    for name in variants:
        passed = outcomes[name]["ok"]
        p50 = statistics.median(timings[name])
        detail = ", ".join(f"{k}={v}" for k, v in sorted(outcomes[name].items()))
        print(f"{name:<14} pass {passed}/{args.runs}  p50={p50:.1f}s  [{detail}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
