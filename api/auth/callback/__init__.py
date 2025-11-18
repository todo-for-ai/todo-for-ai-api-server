# Callback routes
from .index import callback
from .google import google_callback
from .guest import guest_callback

__all__ = [
    'callback',
    'google_callback',
    'guest_callback'
]
