#!/usr/bin/env python3
"""
Simple API benchmark script for local performance comparison.

Usage:
  python scripts/api_benchmark.py --runs 100 --concurrency 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class RequestResult:
    ok: bool
    status_code: int
    duration_ms: float
    error: Optional[str] = None


def fetch_guest_token(base_url: str) -> str:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    url = f"{base_url}/todo-for-ai/api/v1/auth/login/guest?return_to=%2Ftodo-for-ai%2Fpages"
    req = urllib.request.Request(url, method="HEAD")
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as response:
            location = response.headers.get("Location")
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        location = e.headers.get("Location")

    if not location:
        raise RuntimeError("Guest login did not return Location header")

    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    token = qs.get("access_token", [None])[0]
    if not token:
        raise RuntimeError("Guest login redirect missing access_token")
    return token


def send_request(url: str, token: str) -> RequestResult:
    start = time.perf_counter()
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
            return RequestResult(
                ok=200 <= response.status < 300,
                status_code=response.status,
                duration_ms=(time.perf_counter() - start) * 1000,
            )
    except urllib.error.HTTPError as e:
        return RequestResult(
            ok=False,
            status_code=e.code,
            duration_ms=(time.perf_counter() - start) * 1000,
            error=f"HTTP {e.code}",
        )
    except Exception as e:  # noqa: BLE001
        return RequestResult(
            ok=False,
            status_code=0,
            duration_ms=(time.perf_counter() - start) * 1000,
            error=str(e),
        )


def run_benchmark(url: str, token: str, runs: int, concurrency: int) -> Dict[str, object]:
    results: List[RequestResult] = []
    lock = threading.Lock()
    start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(send_request, url, token) for _ in range(runs)]
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)

    total_ms = (time.perf_counter() - start) * 1000
    durations = [r.duration_ms for r in results]
    success = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    p50 = statistics.median(durations) if durations else 0
    p95 = sorted(durations)[int(len(durations) * 0.95) - 1] if durations else 0
    avg = statistics.mean(durations) if durations else 0

    errors: Dict[str, int] = {}
    for r in failed:
        key = r.error or f"HTTP {r.status_code}"
        errors[key] = errors.get(key, 0) + 1

    return {
        "url": url,
        "runs": runs,
        "concurrency": concurrency,
        "total_time_ms": round(total_ms, 2),
        "rps": round((runs / (total_ms / 1000)) if total_ms else 0, 2),
        "ok": len(success),
        "failed": len(failed),
        "error_rate": round((len(failed) / runs) * 100, 2) if runs else 0,
        "latency_ms": {
            "avg": round(avg, 2),
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "max": round(max(durations) if durations else 0, 2),
            "min": round(min(durations) if durations else 0, 2),
        },
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:50110")
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    token = fetch_guest_token(args.base_url)
    endpoints = [
        "/todo-for-ai/api/v1/projects/?status=active&page=1&per_page=100",
        "/todo-for-ai/api/v1/tasks?page=1&per_page=100",
    ]

    report: Dict[str, object] = {
        "base_url": args.base_url,
        "runs": args.runs,
        "concurrency": args.concurrency,
        "results": [],
    }

    for endpoint in endpoints:
        url = f"{args.base_url}{endpoint}"
        report["results"].append(run_benchmark(url, token, args.runs, args.concurrency))

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
