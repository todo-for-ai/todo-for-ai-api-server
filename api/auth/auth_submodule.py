"""
Auth Blueprint Submodule
"""
from flask import Blueprint

# Create the auth blueprint
auth_bp = Blueprint('auth', __name__)

# Import all routes to register them
from . import (
    login,
    callback,
    user,
    verify
)
