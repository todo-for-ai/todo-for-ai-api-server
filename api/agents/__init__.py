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
from . import channels  # noqa: E402
from . import conflicts  # noqa: E402
from . import cross_project  # noqa: E402
from . import dashboard  # noqa: E402
from . import dispatch  # noqa: E402
from . import experiences  # noqa: E402
from . import health  # noqa: E402
from . import knowledge  # noqa: E402
from . import maintenance  # noqa: E402
from . import productivity  # noqa: E402
from . import protocols  # noqa: E402
from . import reputation  # noqa: E402
from . import sandboxes  # noqa: E402
from . import security  # noqa: E402
from . import workflow_runs  # noqa: E402
from . import workflow_versions  # noqa: E402

# ── Re-exports for backward compatibility ──
from ._shared import flush_sse_notifications  # noqa: E402
from .maintenance import _run_orchestration  # noqa: E402
