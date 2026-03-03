"""
Backfill user_activities from tasks table for heatmap testing.

Usage:
  python scripts/backfill_user_activity.py --user-id 1
"""

import argparse
import os
import sys
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


def backfill_user_activity(db, user_id: int) -> None:
    # Rebuild the activity table rows for this user from task create timestamps.
    # This is intended for load/performance test data that bypassed API write paths.
    delete_sql = text("DELETE FROM user_activities WHERE user_id = :uid")
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
        )
        SELECT
            owner_id AS user_id,
            DATE(created_at) AS activity_date,
            COUNT(*) AS task_created_count,
            0 AS task_updated_count,
            0 AS task_status_changed_count,
            0 AS task_completed_count,
            COUNT(*) AS total_activity_count,
            CASE
                WHEN COUNT(*) = 0 THEN 0
                WHEN COUNT(*) <= 2 THEN 1
                WHEN COUNT(*) <= 5 THEN 2
                WHEN COUNT(*) <= 10 THEN 3
                ELSE 4
            END AS activity_level,
            MIN(created_at) AS first_activity_at,
            MAX(created_at) AS last_activity_at,
            NOW() AS created_at,
            NOW() AS updated_at,
            'backfill-script' AS created_by
        FROM tasks
        WHERE owner_id = :uid
        GROUP BY owner_id, DATE(created_at)
        """
    )
    verify_sql = text(
        """
        SELECT activity_date, total_activity_count, activity_level
        FROM user_activities
        WHERE user_id = :uid
        ORDER BY activity_date DESC
        LIMIT 10
        """
    )

    db.session.execute(delete_sql, {"uid": user_id})
    db.session.execute(insert_sql, {"uid": user_id})
    db.session.commit()

    rows = db.session.execute(verify_sql, {"uid": user_id}).fetchall()
    print(f"Backfill completed for user_id={user_id}.")
    for row in rows:
        print(f"  {row.activity_date} count={row.total_activity_count} level={row.activity_level}")


def main():
    parser = argparse.ArgumentParser(description="Backfill user_activities for heatmap testing.")
    parser.add_argument("--user-id", type=int, required=True, help="Target user id")
    args = parser.parse_args()

    app, db = _bootstrap_app()
    with app.app_context():
        backfill_user_activity(db, args.user_id)


if __name__ == "__main__":
    main()
