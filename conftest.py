"""Pytest configuration and shared fixtures for API server tests."""

import pytest
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(scope="session")
def app():
    """Create application for testing."""
    import os
    os.environ['TEST_DATABASE_URL'] = 'sqlite:///:memory:'

    from app import create_app
    from models import db

    app = create_app('testing')
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "JWT_SECRET_KEY": "test-secret-key",
        "WTF_CSRF_ENABLED": False,
    })

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


@pytest.fixture
def runner(app):
    """Create test CLI runner."""
    return app.test_cli_runner()


@pytest.fixture
def db_session(app):
    """Provide database session for tests."""
    from models import db
    with app.app_context():
        yield db.session
        db.session.rollback()


@pytest.fixture
def auth_headers(client):
    """Create authenticated user and return headers."""
    # Register user
    client.post("/api/auth/register", json={
        "username": "testuser",
        "email": "test@example.com",
        "password": "testpassword123"
    })

    # Login
    response = client.post("/api/auth/login", json={
        "username": "testuser",
        "password": "testpassword123"
    })

    token = response.json.get("access_token", "")
    return {"Authorization": f"Bearer {token}"}
