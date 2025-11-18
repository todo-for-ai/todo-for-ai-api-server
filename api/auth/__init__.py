# Auth API Module
from flask import Blueprint

# Import all routes from submodules
from .login.index import login
from .login.google import google_login
from .login.guest import guest_login
from .callback.index import callback
from .callback.google import google_callback
from .callback.guest import guest_callback
from .user.logout import logout, get_current_user_info, update_current_user
from .verify.token import verify_token, refresh_token

# Re-export the blueprint
from .auth_submodule import auth_bp

__all__ = [
    'auth_bp',
    'login',
    'google_login',
    'guest_login',
    'callback',
    'google_callback',
    'guest_callback',
    'logout',
    'get_current_user_info',
    'update_current_user',
    'verify_token',
    'refresh_token'
]
