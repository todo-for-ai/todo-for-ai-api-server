"""
MCP (Model Context Protocol) API package.

Provides HTTP API interface for AI assistants to interact with the todo system.
"""

from flask import Blueprint

mcp_bp = Blueprint('mcp', __name__)

# ── Submodule imports (side-effect: registers routes on mcp_bp) ──
from . import tools  # noqa: E402