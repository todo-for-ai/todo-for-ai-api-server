"""
Agent collaboration API — task operations (legacy re-export shim).

Routes and helpers have been split into:
- task_events.py          (task collaboration events: list/post)
- task_handoffs.py        (task handoff and subtask creation)
- agent_inbox.py          (agent @mention inbox and notifications)
- task_shared_context.py (per-task shared context CRUD)
- run_logs.py            (execution run log append/list)
- _dispatch_helpers.py   (shared dispatch-policy/assignment helpers)

This module re-exports the helpers and route functions that other packages
may still import from ``task_operations`` for backward compatibility. The
Flask routes are registered by importing the new submodules in
``api/agents/__init__.py``; the names below are kept importable so existing
``from .task_operations import X`` statements elsewhere keep working.
"""

# ── Re-export shared dispatch/assignment helpers ──
from ._dispatch_helpers import (  # noqa: F401
    DISPATCH_MAX_ASSIGNMENTS,
    DISPATCH_PREVIEW_CANDIDATE_LIMIT,
    DISPATCH_POLICY_DEFAULTS,
    normalize_dispatch_policy,
    get_coordinator_dispatch_policy,
    resolve_dispatch_options,
    collect_claimable_tasks,
    find_available_worker_agents,
    serialize_dispatch_candidate,
    create_assignment_with_run,
    cancel_assignment_for_handoff,
)

# ── Re-export route functions (for any legacy direct imports) ──
from .task_events import list_task_events, post_task_event  # noqa: F401
from .task_handoffs import handoff_task, create_subtask  # noqa: F401
from .agent_inbox import (  # noqa: F401
    agent_inbox,
    list_notifications,
    mark_notifications_read,
)
from .task_shared_context import (  # noqa: F401
    list_shared_context,
    upsert_shared_context,
    delete_shared_context,
)
from .run_logs import list_run_logs, append_run_logs  # noqa: F401
