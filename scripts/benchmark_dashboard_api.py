#!/usr/bin/env python3
"""
Benchmark dashboard API endpoints with a temporary API token.

Usage:
  PYTHONPATH=. .venv/bin/python scripts/benchmark_dashboard_api.py --runs 5 --org-user-id 1
"""

import argparse
import statistics
import time
from typing import Optional

from app import create_app
from models import db, ApiToken, User, UserStatus


def _pick_user(user_id: Optional[int]):
    if user_id:
        return User.query.get(user_id)
    return User.query.filter(User.status == UserStatus.ACTIVE).order_by(User.id.asc()).first()


def _create_temp_token(user):
    api_token, token = ApiToken.generate_token(
        name="dashboard-benchmark",
        description="temporary token for dashboard benchmark",
        expires_days=1,
    )
    api_token.user_id = user.id
    db.session.add(api_token)
    db.session.commit()
    return api_token, token


def _delete_token(token_id):
    ApiToken.query.filter(ApiToken.id == token_id).delete()
    db.session.commit()


def _timed_requests(client, path: str, headers: dict, runs: int):
    timings = []
    for _ in range(runs):
        start = time.perf_counter()
        response = client.get(path, headers=headers)
        end = time.perf_counter()
        if response.status_code != 200:
            raise RuntimeError(f"{path} failed: {response.status_code} {response.get_json()}")
        timings.append((end - start) * 1000)
    return timings


def _report(label: str, timings: list[float]):
    if not timings:
        return
    p50 = statistics.median(timings)
    p95 = statistics.quantiles(timings, n=20)[18] if len(timings) >= 20 else max(timings)
    avg = sum(timings) / len(timings)
    print(f"{label}: avg={avg:.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms runs={len(timings)}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark dashboard API endpoints.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        user = _pick_user(args.user_id)
        if not user:
            raise RuntimeError("No active user found for benchmark.")
        api_token, token = _create_temp_token(user)
        token_id = api_token.id

    headers = {"Authorization": f"Bearer {token}"}
    stats_path = "/todo-for-ai/api/v1/dashboard/stats"
    heatmap_path = "/todo-for-ai/api/v1/dashboard/activity-heatmap"
    summary_path = "/todo-for-ai/api/v1/dashboard/activity-summary"

    with app.test_client() as client:
        # Cold run
        cold_stats = _timed_requests(client, stats_path, headers, runs=1)
        cold_heatmap = _timed_requests(client, heatmap_path, headers, runs=1)
        cold_summary = _timed_requests(client, summary_path, headers, runs=1)

        # Warm run
        warm_stats = _timed_requests(client, stats_path, headers, runs=args.runs)
        warm_heatmap = _timed_requests(client, heatmap_path, headers, runs=args.runs)
        warm_summary = _timed_requests(client, summary_path, headers, runs=args.runs)

    with app.app_context():
        _delete_token(token_id)

    print("== Cold ==")
    _report("dashboard/stats", cold_stats)
    _report("dashboard/activity-heatmap", cold_heatmap)
    _report("dashboard/activity-summary", cold_summary)
    print("== Warm ==")
    _report("dashboard/stats", warm_stats)
    _report("dashboard/activity-heatmap", warm_heatmap)
    _report("dashboard/activity-summary", warm_summary)


if __name__ == "__main__":
    main()
