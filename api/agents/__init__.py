"""
Agent collaboration API package.

Routes are split across submodules; each imports ``agents_bp`` from this
package and registers its own routes.  Importing a submodule is enough —
the side-effect of decorating functions onto the blueprint happens at
import time.
"""

from flask import Blueprint

agents_bp = Blueprint("agents", __name__)

# ── Submodule imports (side-effect: registers routes on agents_bp) ──
from . import _core  # noqa: E402  (must come after agents_bp definition)
from . import analytics  # noqa: E402
from . import analytics_capability  # noqa: E402
from . import analytics_collaboration  # noqa: E402
from . import channels  # noqa: E402
from . import conflicts  # noqa: E402
from . import cross_project  # noqa: E402
from . import dashboard  # noqa: E402
from . import dispatch  # noqa: E402
from . import experiences  # noqa: E402
from . import experience_analytics  # noqa: E402
from . import experience_decay  # noqa: E402
from . import failure_analysis  # noqa: E402
from . import health  # noqa: E402
from . import knowledge  # noqa: E402
from . import maintenance  # noqa: E402
from . import messaging  # noqa: E402
from . import workflow_triggers  # noqa: E402
from . import workflow_templates  # noqa: E402
from . import collaboration_templates  # noqa: E402
from . import productivity  # noqa: E402
from . import protocols  # noqa: E402
from . import reputation  # noqa: E402
from . import sandboxes  # noqa: E402
from . import security  # noqa: E402
from . import task_events  # noqa: E402
from . import task_handoffs  # noqa: E402
from . import agent_inbox  # noqa: E402
from . import task_shared_context  # noqa: E402
from . import run_logs  # noqa: E402
from . import task_operations  # noqa: E402  (legacy re-export shim, kept for back-compat)
from . import workflow_analytics  # noqa: E402
from . import workflow_runs  # noqa: E402
from . import workflow_versions  # noqa: E402
from . import task_templates  # noqa: E402
from . import workflow_routes  # noqa: E402
from . import project_members  # noqa: E402
from . import analytics_workflow  # noqa: E402
from . import skill_profile  # noqa: E402
from . import memory_governance  # noqa: E402
from . import working_schedule  # noqa: E402

# ── Re-exports for backward compatibility ──
from ._shared import flush_sse_notifications  # noqa: E402
from .maintenance import _run_orchestration  # noqa: E402
