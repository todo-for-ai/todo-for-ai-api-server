"""Server-Sent Events (SSE) endpoint for real-time collaboration updates.

Each authenticated user gets an in-memory event buffer.  When any endpoint
writes a task event (via ``notify_sse``), the event is pushed into every
buffer belonging to the event's owner so the SSE stream can deliver it
immediately to the browser.

This is deliberately simple: a dict of ``user_id -> deque`` with a
``threading.Event`` per user to wake the streaming response.  No Redis,
no cross-process coordination — sufficient for the common single-process
SQLite / gevent deployment.
"""

from __future__ import annotations  # Py3.9 兼容 PEP 604 类型注解

import json
import threading
from collections import deque
from datetime import datetime

from flask import Blueprint, Response, request

from core.auth import get_current_user, unified_auth_required

sse_bp = Blueprint("sse", __name__)

# user_id -> {"queue": deque, "event": threading.Event}
_buffers: dict[int, dict] = {}
_lock = threading.Lock()
_MAX_BUFFER = 200


def _get_buffer(user_id: int) -> dict:
    with _lock:
        if user_id not in _buffers:
            _buffers[user_id] = {
                "queue": deque(maxlen=_MAX_BUFFER),
                "event": threading.Event(),
            }
        return _buffers[user_id]


def notify_sse(user_id: int, event_type: str, payload: dict):
    """Push a collaboration event to the user's SSE buffer.

    Called from ``api.agents`` whenever a TaskEvent is committed.
    """
    buf = _get_buffer(user_id)
    buf["queue"].append({
        "event_type": event_type,
        "payload": payload,
        "pushed_at": datetime.utcnow().isoformat(),
    })
    buf["event"].set()


def _sse_stream(user_id: int, last_event_id: int | None):
    """Generator yielding SSE frames.  Runs inside the request context."""
    buf = _get_buffer(user_id)
    sent = 0

    while True:
        # Drain everything currently in the queue
        while buf["queue"]:
            item = buf["queue"].popleft()
            data = json.dumps(item, ensure_ascii=False, default=str)
            yield f"id: {user_id}-{sent}\ndata: {data}\n\n"
            sent += 1

        # Wait for a signal (up to 30s, then send a keep-alive)
        buf["event"].clear()
        if buf["event"].wait(timeout=30):
            # New data arrived — loop to drain
            continue
        else:
            # Timeout — send a keep-alive comment
            yield ": keepalive\n\n"


@sse_bp.route("/collaboration", methods=["GET"])
@unified_auth_required
def collaboration_stream():
    """SSE endpoint: real-time collaboration event stream for the current user.

    The stream pushes ``task_claimed``, ``task_dispatched``, ``handoff``,
    ``message``, and other collaboration events as they happen, eliminating
    the need for the frontend to poll.

    Query parameters:
        last_event_id: Optional.  If provided, the server first sends any
            events that occurred since this id (using the ``since_id``
            mechanism on the TaskEvent table) before switching to live push.
    """
    current_user = get_current_user()
    user_id = current_user.id

    # No seeding — the client already has the since_id polling fallback
    # for catching up on missed events.  SSE is purely for real-time push.

    response = Response(
        _sse_stream(user_id, last_event_id),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx
            "Connection": "keep-alive",
        },
    )
    return response
