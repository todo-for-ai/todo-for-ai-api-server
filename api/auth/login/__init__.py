# Login routes
from .index import login
from .google import google_login
from .guest import guest_login

__all__ = [
    'login',
    'google_login',
    'guest_login'
]
