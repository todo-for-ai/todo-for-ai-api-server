"""Shared constants for organizations APIs."""

SYSTEM_ROLE_DEFINITIONS = [
    {'key': 'owner', 'name': 'Owner', 'description': 'Organization owner'},
    {'key': 'admin', 'name': 'Admin', 'description': 'Organization admin'},
    {'key': 'member', 'name': 'Member', 'description': 'Organization member'},
    {'key': 'viewer', 'name': 'Viewer', 'description': 'Read-only member'},
]

ROLE_PRIORITY = ['owner', 'admin', 'member', 'viewer']
