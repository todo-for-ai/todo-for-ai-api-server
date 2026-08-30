"""Organizations API package."""

from flask import Blueprint

organizations_bp = Blueprint('organizations', __name__)

# Ensure route decorators register on blueprint import.
from . import routes_organizations  # noqa: E402,F401
from . import routes_roles  # noqa: E402,F401
from . import routes_members  # noqa: E402,F401
from . import routes_events  # noqa: E402,F401

__all__ = ['organizations_bp']
