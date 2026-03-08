"""Agent automation API package."""

import hashlib

from flask import Blueprint

from .shared import _compute_next_fire_at

agent_automation_bp = Blueprint('agent_automation', __name__)

# Ensure route decorators are registered on import.
from . import routes_runner  # noqa: E402,F401
from . import routes_triggers  # noqa: E402,F401
from . import routes_runs  # noqa: E402,F401
from . import routes_channels  # noqa: E402,F401

def make_trigger_idempotency_key(trigger, reason, payload):
    digest = hashlib.sha256(f"{trigger.id}:{reason}:{payload}".encode('utf-8')).hexdigest()
    return f"trg:{trigger.id}:{digest[:32]}"

__all__ = ['agent_automation_bp', '_compute_next_fire_at', 'make_trigger_idempotency_key']
