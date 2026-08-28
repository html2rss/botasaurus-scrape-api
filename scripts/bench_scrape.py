#!/usr/bin/env python3
"""Time POST /scrape happy path and print wall-time percentiles."""

from __future__ import annotations

import argparse
import statistics
import time

from fastapi.testclient import TestClient

from app.main import create_app


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bench POST /scrape via TestClient")
    parser.add_argument("--runs", type=int, default=5, help="Number of timed runs")
    parser.add_argument(
        "--url",
        default="https://example.com",
        help="Target URL for the scrape payload",
    )
    parser.add_argument(
        "--execution-mode",
        choices=("request", "browser", "auto"),
        default="request",
        help=(
            "Scrape execution_mode. Use browser with SCRAPE_PREWARM=true in Docker "
            "(--shm-size=1gb --init) to compare cold vs warm boot_ms from scrape_boot logs."
        ),
    )
    args = parser.parse_args(argv)

    app = create_app()
    durations_ms: list[float] = []
    render_ms_values: list[int] = []

    with TestClient(app) as client:
        for _ in range(args.runs):
            started = time.perf_counter()
            response = client.post(
                "/scrape",
                json={"url": args.url, "execution_mode": args.execution_mode},
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            durations_ms.append(elapsed_ms)
            if response.status_code == 200:
                body = response.json()
                render_ms_values.append(
                    int(body.get("diagnostics", {}).get("render_ms", 0))
                )

    p50 = percentile(durations_ms, 50)
    p95 = percentile(durations_ms, 95)
    print(
        f"bench_runs={args.runs} execution_mode={args.execution_mode} "
        f"wall_ms_p50={p50:.1f} wall_ms_p95={p95:.1f}"
    )
    if render_ms_values:
        print(
            "render_ms_p50="
            f"{statistics.median(render_ms_values):.0f} "
            f"render_ms_max={max(render_ms_values)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
