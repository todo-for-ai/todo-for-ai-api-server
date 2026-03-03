"""
Generate realistic user_activities rows for dashboard heatmap testing.

This script is intentionally independent from tasks/task_history so it can be
used to stress-test frontend and dashboard APIs even when bulk task imports
bypassed normal activity write paths.

Usage:
  .venv/bin/python scripts/generate_realistic_user_activity.py --user-id 1
"""

import argparse
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import text


def _bootstrap_app():
    backend_root = Path(__file__).resolve().parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app import create_app
    from models import db

    app = create_app()
    return app, db


def _activity_level(total: int) -> int:
    if total <= 0:
        return 0
    if total <= 2:
        return 1
    if total <= 5:
        return 2
    if total <= 10:
        return 3
    return 4


def _split_activity(total: int, rng: random.Random):
    if total <= 0:
        return 0, 0, 0, 0

    # Keep realistic proportions with small randomness.
    w_created = rng.uniform(0.16, 0.30)
    w_updated = rng.uniform(0.28, 0.46)
    w_status = rng.uniform(0.14, 0.28)
    w_completed = rng.uniform(0.08, 0.22)
    weight_sum = w_created + w_updated + w_status + w_completed

    created = int(total * (w_created / weight_sum))
    updated = int(total * (w_updated / weight_sum))
    status_changed = int(total * (w_status / weight_sum))
    completed = max(0, total - created - updated - status_changed)
    return created, updated, status_changed, completed


def _random_activity_window(day: date, total: int, rng: random.Random):
    if total <= 0:
        return None, None

    start_hour = rng.randint(8, 11)
    start_minute = rng.randint(0, 59)
    first_at = datetime.combine(day, time(start_hour, start_minute))

    # Active window 1-10 hours depending on intensity
    duration_minutes = min(10 * 60, 60 + int(total ** 0.5) * rng.randint(10, 20))
    last_at = first_at + timedelta(minutes=duration_minutes)
    end_of_day = datetime.combine(day, time(23, 59, 59))
    if last_at > end_of_day:
        last_at = end_of_day

    return first_at, last_at


def generate_rows(days: int, base_daily: int, seed: int):
    rng = random.Random(seed)
    end_day = date.today()
    start_day = end_day - timedelta(days=days - 1)

    rows = []
    current = start_day
    idx = 0
    while current <= end_day:
        # Workday/weekend seasonality + gentle trend + random noise.
        weekday = current.weekday()  # 0=Mon ... 6=Sun
        weekday_factor = 1.0 if weekday <= 4 else (0.6 if weekday == 5 else 0.5)
        trend_factor = 0.85 + 0.30 * (idx / max(days - 1, 1))
        holiday_dip = 0.55 if rng.random() < 0.04 else 1.0
        mean = base_daily * weekday_factor * trend_factor * holiday_dip
        stddev = max(3.0, mean * 0.28)
        total = max(0, int(rng.gauss(mean, stddev)))
        if rng.random() < 0.06:
            total = 0

        created, updated, status_changed, completed = _split_activity(total, rng)
        level = _activity_level(total)
        first_at, last_at = _random_activity_window(current, total, rng)

        rows.append({
            "activity_date": current,
            "task_created_count": created,
            "task_updated_count": updated,
            "task_status_changed_count": status_changed,
            "task_completed_count": completed,
            "total_activity_count": total,
            "activity_level": level,
            "first_activity_at": first_at,
            "last_activity_at": last_at,
        })

        idx += 1
        current += timedelta(days=1)
    return rows


def rebuild_user_activities(db, user_id: int, rows):
    db.session.execute(text("DELETE FROM user_activities WHERE user_id = :uid"), {"uid": user_id})
    insert_sql = text(
        """
        INSERT INTO user_activities (
            user_id,
            activity_date,
            task_created_count,
            task_updated_count,
            task_status_changed_count,
            task_completed_count,
            total_activity_count,
            activity_level,
            first_activity_at,
            last_activity_at,
            created_at,
            updated_at,
            created_by
        ) VALUES (
            :user_id,
            :activity_date,
            :task_created_count,
            :task_updated_count,
            :task_status_changed_count,
            :task_completed_count,
            :total_activity_count,
            :activity_level,
            :first_activity_at,
            :last_activity_at,
            NOW(),
            NOW(),
            'synthetic-activity-generator'
        )
        """
    )

    payload = []
    for row in rows:
        payload.append({"user_id": user_id, **row})
    db.session.execute(insert_sql, payload)
    db.session.commit()


def main():
    parser = argparse.ArgumentParser(description="Generate realistic user_activities data.")
    parser.add_argument("--user-id", type=int, required=True, help="Target user id")
    parser.add_argument("--days", type=int, default=365, help="How many recent days to generate")
    parser.add_argument("--base-daily", type=int, default=280, help="Baseline daily activity")
    parser.add_argument("--seed", type=int, default=20260302, help="Random seed for reproducible output")
    args = parser.parse_args()

    app, db = _bootstrap_app()
    with app.app_context():
        rows = generate_rows(days=args.days, base_daily=args.base_daily, seed=args.seed)
        rebuild_user_activities(db, args.user_id, rows)

        summary_sql = text(
            """
            SELECT
                COUNT(*) AS days,
                SUM(total_activity_count) AS total_activities,
                AVG(total_activity_count) AS avg_daily,
                MAX(total_activity_count) AS max_daily
            FROM user_activities
            WHERE user_id = :uid
            """
        )
        summary = db.session.execute(summary_sql, {"uid": args.user_id}).mappings().first()
        print(
            f"Generated user_activities for user_id={args.user_id}: "
            f"days={summary['days']}, total={summary['total_activities']}, "
            f"avg={float(summary['avg_daily'] or 0):.2f}, max={summary['max_daily']}"
        )


if __name__ == "__main__":
    main()
