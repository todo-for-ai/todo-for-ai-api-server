"""Users API package."""

from flask import Blueprint

users_bp = Blueprint('users', __name__)

# Ensure route decorators register on blueprint import.
from . import routes_search  # noqa: E402,F401

__all__ = ['users_bp']
