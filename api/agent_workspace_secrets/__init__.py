"""
Workspace Agent Secrets API
"""

from flask import Blueprint

agent_workspace_secrets_bp = Blueprint('agent_workspace_secrets', __name__)

# Ensure route decorators are registered.
from . import routes_collaboration, routes_secrets  # noqa: E402,F401

__all__ = ['agent_workspace_secrets_bp']
