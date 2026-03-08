"""
MCP (Model Context Protocol) HTTP API package.
"""

from flask import Blueprint

from .shared import project_stats_cache

mcp_bp = Blueprint('mcp', __name__)

# Ensure route decorators are registered on import.
from . import routes  # noqa: E402,F401

__all__ = ['mcp_bp', 'project_stats_cache']
