"""Built-in background scheduler for the global collaboration orchestrator.

When enabled via ORCHESTRATOR_ENABLED=true, a daemon thread periodically runs
the full orchestration cycle inside an app context, removing the need for an
external cron job.

Multi-worker deployments: only enable this in ONE worker (e.g. via a
per-worker env var) to avoid duplicate execution. The scheduler uses a
process-local flag, so it does not coordinate across processes/gunicorn
workers.
"""
import threading
import logging

logger = logging.getLogger(__name__)

_scheduler_thread = None
_scheduler_lock = threading.Lock()
_stop_event = threading.Event()
_last_run = None  # dict: {started_at, duration_seconds, summary} of last cycle


def _orchestration_loop(app, user_id, interval):
    """Target of the scheduler thread."""
    from models import User
    from api.agents import _run_orchestration

    while not _stop_event.wait(interval):
        try:
            with app.app_context():
                user = User.query.get(user_id) if user_id else None
                if not user:
                    logger.warning("[ORCHESTRATOR] Configured user not found (id=%s); skipping cycle", user_id)
                    continue
                report, duration, message = _run_orchestration(user, actor_type="system")
                global _last_run
                _last_run = {
                    "summary": message,
                    "duration_seconds": round(duration, 3),
                    "stale_agents": report.get("stale_agents", 0),
                    "timed_out_steps": report.get("timed_out_steps", 0),
                    "triggers_fired": report.get("triggers_fired", 0),
                    "trigger_run_ids": report.get("trigger_run_ids", []),
                    "conflicts_auto_resolved": report.get("conflicts_auto_resolved", 0),
                    "error_count": len(report.get("errors", [])),
                }
                logger.info("[ORCHESTRATOR] %s", message)
        except Exception as e:
            logger.exception("[ORCHESTRATOR] Cycle failed: %s", e)


def start_scheduler(app):
    """Start the orchestrator background thread if enabled in config."""
    global _scheduler_thread, _stop_event

    enabled = app.config.get("ORCHESTRATOR_ENABLED", False)
    if not enabled:
        return False

    interval = max(30, int(app.config.get("ORCHESTRATOR_INTERVAL_SECONDS", 300)))
    user_id = int(app.config.get("ORCHESTRATOR_USER_ID", 0) or 0)
    if not user_id:
        logger.warning("[ORCHESTRATOR] ORCHESTRATOR_USER_ID not set; scheduler will not start (no owner scope).")
        return False

    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        _stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_orchestration_loop,
            args=(app, user_id, interval),
            name="orchestrator-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()
        logger.info("[ORCHESTRATOR] Scheduler started: interval=%ss user_id=%s", interval, user_id)
        return True


def stop_scheduler():
    """Signal the scheduler thread to stop (best-effort; for tests/shutdown)."""
    global _scheduler_thread
    _stop_event.set()
    thread = _scheduler_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    _scheduler_thread = None


def scheduler_status():
    """Return the current scheduler state + last run summary."""
    return {
        "enabled": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "last_run": _last_run,
    }
