"""
Simulate realistic task lifecycle events for a user in a safe, batched scope.

This script updates a recent sample of tasks, generates task_history rows, and
rebuilds user_activities from tasks + history events.

Usage:
  .venv/bin/python scripts/simulate_realistic_task_events.py --user-id 1
"""

import argparse
import sys
from datetime import datetime, timedelta
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


def _rebuild_user_activities(db, user_id: int, days: int):
    start_dt = datetime.utcnow() - timedelta(days=days - 1)
    start_date = start_dt.date()

    created_rows = db.session.execute(
        text(
            """
            SELECT
                DATE(created_at) AS d,
                COUNT(*) AS created_cnt,
                MIN(created_at) AS first_at,
                MAX(created_at) AS last_at
            FROM tasks
            WHERE owner_id = :uid AND created_at >= :start_dt
            GROUP BY DATE(created_at)
            """
        ),
        {"uid": user_id, "start_dt": start_dt},
    ).mappings().all()

    history_rows = db.session.execute(
        text(
            """
            SELECT
                DATE(th.changed_at) AS d,
                SUM(th.action = 'UPDATED') AS updated_cnt,
                SUM(th.action = 'STATUS_CHANGED') AS status_changed_cnt,
                SUM(th.action = 'COMPLETED') AS completed_cnt,
                MIN(th.changed_at) AS first_at,
                MAX(th.changed_at) AS last_at
            FROM task_history th
            JOIN tasks t ON t.id = th.task_id
            WHERE t.owner_id = :uid
              AND th.changed_at >= :start_dt
            GROUP BY DATE(th.changed_at)
            """
        ),
        {"uid": user_id, "start_dt": start_dt},
    ).mappings().all()

    daily = {}
    for row in created_rows:
        d = row["d"]
        daily[d] = {
            "task_created_count": int(row["created_cnt"] or 0),
            "task_updated_count": 0,
            "task_status_changed_count": 0,
            "task_completed_count": 0,
            "first_activity_at": row["first_at"],
            "last_activity_at": row["last_at"],
        }

    for row in history_rows:
        d = row["d"]
        if d not in daily:
            daily[d] = {
                "task_created_count": 0,
                "task_updated_count": 0,
                "task_status_changed_count": 0,
                "task_completed_count": 0,
                "first_activity_at": row["first_at"],
                "last_activity_at": row["last_at"],
            }

        daily[d]["task_updated_count"] = int(row["updated_cnt"] or 0)
        daily[d]["task_status_changed_count"] = int(row["status_changed_cnt"] or 0)
        daily[d]["task_completed_count"] = int(row["completed_cnt"] or 0)

        if row["first_at"] and (
            not daily[d]["first_activity_at"] or row["first_at"] < daily[d]["first_activity_at"]
        ):
            daily[d]["first_activity_at"] = row["first_at"]
        if row["last_at"] and (
            not daily[d]["last_activity_at"] or row["last_at"] > daily[d]["last_activity_at"]
        ):
            daily[d]["last_activity_at"] = row["last_at"]

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
            'simulate-realistic-task-events'
        )
        """
    )

    payload = []
    for d, item in daily.items():
        total = (
            item["task_created_count"]
            + item["task_updated_count"]
            + item["task_status_changed_count"]
            + item["task_completed_count"]
        )
        if total <= 0:
            level = 0
        elif total <= 2:
            level = 1
        elif total <= 5:
            level = 2
        elif total <= 10:
            level = 3
        else:
            level = 4
        payload.append(
            {
                "user_id": user_id,
                "activity_date": d,
                "task_created_count": item["task_created_count"],
                "task_updated_count": item["task_updated_count"],
                "task_status_changed_count": item["task_status_changed_count"],
                "task_completed_count": item["task_completed_count"],
                "total_activity_count": total,
                "activity_level": level,
                "first_activity_at": item["first_activity_at"],
                "last_activity_at": item["last_activity_at"],
            }
        )

    if payload:
        db.session.execute(insert_sql, payload)
    db.session.commit()


def simulate_events(
    db,
    user_id: int,
    sample_size: int,
    lookback_days: int,
    history_days: int,
    rebuild_activities: bool,
    id_min: int = None,
    id_max: int = None,
):
    sample_size = int(sample_size)
    lookback_days = int(lookback_days)
    history_days = int(history_days)
    if sample_size <= 0 or lookback_days <= 0 or history_days <= 0:
        raise ValueError("sample_size/lookback_days/history_days must be positive integers")

    db.session.execute(text("DROP TEMPORARY TABLE IF EXISTS tmp_seed_task_ids"))
    db.session.execute(
        text(
            """
            CREATE TEMPORARY TABLE tmp_seed_task_ids (
                id BIGINT NOT NULL PRIMARY KEY
            ) ENGINE=MEMORY
            """
        )
    )
    insert_sql = """
        INSERT INTO tmp_seed_task_ids (id)
        SELECT id
        FROM tasks
        WHERE owner_id = :uid
    """
    bind_params = {"uid": user_id, "sample_size": sample_size}
    if id_min is not None:
        insert_sql += " AND id >= :id_min"
        bind_params["id_min"] = id_min
    if id_max is not None:
        insert_sql += " AND id <= :id_max"
        bind_params["id_max"] = id_max
    insert_sql += " ORDER BY id DESC LIMIT :sample_size"
    db.session.execute(text(insert_sql), bind_params)

    # Update sampled tasks with deterministic yet varied lifecycle fields.
    update_sql = text(
        f"""
        UPDATE tasks t
        JOIN tmp_seed_task_ids s ON s.id = t.id
        SET
            t.status = CASE
                WHEN MOD(t.id, 100) < 42 THEN 'DONE'
                WHEN MOD(t.id, 100) < 63 THEN 'IN_PROGRESS'
                WHEN MOD(t.id, 100) < 78 THEN 'REVIEW'
                WHEN MOD(t.id, 100) < 96 THEN 'TODO'
                ELSE 'CANCELLED'
            END,
            t.priority = CASE
                WHEN MOD(t.id, 100) < 10 THEN 'URGENT'
                WHEN MOD(t.id, 100) < 30 THEN 'HIGH'
                WHEN MOD(t.id, 100) < 75 THEN 'MEDIUM'
                ELSE 'LOW'
            END,
            t.is_ai_task = CASE WHEN MOD(t.id, 10) < 7 THEN 1 ELSE 0 END,
            t.created_at = TIMESTAMP(
                DATE_SUB(UTC_DATE(), INTERVAL MOD(t.id * 17, {lookback_days}) DAY),
                MAKETIME(MOD(t.id * 7, 24), MOD(t.id * 11, 60), MOD(t.id * 13, 60))
            ),
            t.updated_at = LEAST(
                UTC_TIMESTAMP(),
                DATE_ADD(
                    TIMESTAMP(
                        DATE_SUB(UTC_DATE(), INTERVAL MOD(t.id * 17, {lookback_days}) DAY),
                        MAKETIME(MOD(t.id * 7, 24), MOD(t.id * 11, 60), MOD(t.id * 13, 60))
                    ),
                    INTERVAL MOD(t.id * 19, 2160) MINUTE
                )
            ),
            t.completed_at = CASE
                WHEN MOD(t.id, 100) < 42 THEN LEAST(
                    UTC_TIMESTAMP(),
                    DATE_ADD(
                        TIMESTAMP(
                            DATE_SUB(UTC_DATE(), INTERVAL MOD(t.id * 17, {lookback_days}) DAY),
                            MAKETIME(MOD(t.id * 7, 24), MOD(t.id * 11, 60), MOD(t.id * 13, 60))
                        ),
                        INTERVAL MOD(t.id * 23, 10080) MINUTE
                    )
                )
                ELSE NULL
            END,
            t.due_date = CASE
                WHEN MOD(t.id, 100) < 42 OR MOD(t.id, 100) >= 96 THEN NULL
                ELSE DATE_ADD(
                    TIMESTAMP(
                        DATE_SUB(UTC_DATE(), INTERVAL MOD(t.id * 17, {lookback_days}) DAY),
                        MAKETIME(MOD(t.id * 7, 24), MOD(t.id * 11, 60), MOD(t.id * 13, 60))
                    ),
                    INTERVAL (MOD(t.id * 29, 45) + 1) DAY
                )
            END,
            t.completion_rate = CASE
                WHEN MOD(t.id, 100) < 42 THEN 100
                WHEN MOD(t.id, 100) < 63 THEN 45
                WHEN MOD(t.id, 100) < 78 THEN 78
                WHEN MOD(t.id, 100) < 96 THEN 15
                ELSE 0
            END
        WHERE t.owner_id = :uid
        """
    )
    db.session.execute(update_sql, {"uid": user_id})

    # Rebuild task history for sampled tasks.
    db.session.execute(
        text(
            """
            DELETE th
            FROM task_history th
            JOIN tmp_seed_task_ids s ON s.id = th.task_id
            """
        )
    )
    db.session.execute(
        text(
            """
            INSERT INTO task_history (
                task_id, action, field_name, old_value, new_value, changed_by, changed_at, comment
            )
            SELECT
                t.id,
                'CREATED',
                NULL,
                NULL,
                NULL,
                'seed-script',
                t.created_at,
                'Task created during realistic simulation'
            FROM tasks t
            JOIN tmp_seed_task_ids s ON s.id = t.id
            """
        )
    )
    db.session.execute(
        text(
            """
            INSERT INTO task_history (
                task_id, action, field_name, old_value, new_value, changed_by, changed_at, comment
            )
            SELECT
                t.id,
                'UPDATED',
                'content',
                NULL,
                'refined',
                'seed-script',
                DATE_ADD(t.created_at, INTERVAL MOD(t.id * 3, 720) MINUTE),
                'Routine task update'
            FROM tasks t
            JOIN tmp_seed_task_ids s ON s.id = t.id
            WHERE MOD(t.id, 100) < 88
            """
        )
    )
    db.session.execute(
        text(
            """
            INSERT INTO task_history (
                task_id, action, field_name, old_value, new_value, changed_by, changed_at, comment
            )
            SELECT
                t.id,
                'STATUS_CHANGED',
                'status',
                'TODO',
                t.status,
                'seed-script',
                DATE_ADD(t.created_at, INTERVAL MOD(t.id * 5, 1440) MINUTE),
                'Task status changed during simulation'
            FROM tasks t
            JOIN tmp_seed_task_ids s ON s.id = t.id
            WHERE t.status IN ('IN_PROGRESS', 'REVIEW', 'DONE', 'CANCELLED')
            """
        )
    )
    db.session.execute(
        text(
            """
            INSERT INTO task_history (
                task_id, action, field_name, old_value, new_value, changed_by, changed_at, comment
            )
            SELECT
                t.id,
                'COMPLETED',
                'status',
                'REVIEW',
                'DONE',
                'seed-script',
                t.completed_at,
                'Task completed in simulation'
            FROM tasks t
            JOIN tmp_seed_task_ids s ON s.id = t.id
            WHERE t.status = 'DONE' AND t.completed_at IS NOT NULL
            """
        )
    )
    db.session.commit()

    if rebuild_activities:
        _rebuild_user_activities(db, user_id=user_id, days=history_days)

    sample_summary = db.session.execute(
        text(
            """
            SELECT
                COUNT(*) AS sampled_tasks,
                SUM(t.status = 'TODO') AS todo_cnt,
                SUM(t.status = 'IN_PROGRESS') AS in_progress_cnt,
                SUM(t.status = 'REVIEW') AS review_cnt,
                SUM(t.status = 'DONE') AS done_cnt,
                SUM(t.status = 'CANCELLED') AS cancelled_cnt
            FROM tasks t
            JOIN tmp_seed_task_ids s ON s.id = t.id
            """
        )
    ).mappings().first()

    history_summary = db.session.execute(
        text(
            """
            SELECT
                COUNT(*) AS history_rows,
                SUM(action = 'CREATED') AS created_rows,
                SUM(action = 'UPDATED') AS updated_rows,
                SUM(action = 'STATUS_CHANGED') AS status_changed_rows,
                SUM(action = 'COMPLETED') AS completed_rows
            FROM task_history
            WHERE changed_by = 'seed-script'
            """
        )
    ).mappings().first()

    output = [
        "Simulation complete:",
        f"  sampled_tasks={sample_summary['sampled_tasks']}",
        "  status(todo/in_progress/review/done/cancelled)="
        f"{sample_summary['todo_cnt']}/{sample_summary['in_progress_cnt']}/"
        f"{sample_summary['review_cnt']}/{sample_summary['done_cnt']}/{sample_summary['cancelled_cnt']}",
        "  history_rows(created/updated/status_changed/completed)="
        f"{history_summary['history_rows']} ({history_summary['created_rows']}/"
        f"{history_summary['updated_rows']}/{history_summary['status_changed_rows']}/"
        f"{history_summary['completed_rows']})",
    ]

    if rebuild_activities:
        activity_summary = db.session.execute(
            text(
                """
                SELECT
                    COUNT(*) AS activity_days,
                    SUM(total_activity_count) AS total_activities,
                    AVG(total_activity_count) AS avg_daily,
                    MAX(total_activity_count) AS max_daily
                FROM user_activities
                WHERE user_id = :uid
                """
            ),
            {"uid": user_id},
        ).mappings().first()
        output.append(
            "  activity(days/total/avg/max)="
            f"{activity_summary['activity_days']}/{activity_summary['total_activities']}/"
            f"{float(activity_summary['avg_daily'] or 0):.2f}/{activity_summary['max_daily']}"
        )

    print("\n".join(output))


def main():
    parser = argparse.ArgumentParser(description="Simulate realistic task events for testing.")
    parser.add_argument("--user-id", type=int, required=True, help="Target user id")
    parser.add_argument("--sample-size", type=int, default=120000, help="How many recent tasks to mutate")
    parser.add_argument("--lookback-days", type=int, default=240, help="Task timeline spread window")
    parser.add_argument("--history-days", type=int, default=365, help="Rebuild activities window")
    parser.add_argument("--rebuild-activities", action="store_true", help="Rebuild user_activities from tasks/history")
    parser.add_argument("--id-min", type=int, default=None, help="Lower bound of task id to include")
    parser.add_argument("--id-max", type=int, default=None, help="Upper bound of task id to include")
    args = parser.parse_args()

    app, db = _bootstrap_app()
    with app.app_context():
        simulate_events(
            db=db,
            user_id=args.user_id,
            sample_size=args.sample_size,
            lookback_days=args.lookback_days,
            history_days=args.history_days,
            rebuild_activities=args.rebuild_activities,
            id_min=args.id_min,
            id_max=args.id_max,
        )


if __name__ == "__main__":
    main()
