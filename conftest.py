"""Pytest configuration and shared fixtures for API server tests."""

import os
import sys
from pathlib import Path

# CRITICAL: Set test environment variables BEFORE any imports
# This must happen before config.py is loaded
os.environ['FLASK_ENV'] = 'testing'
os.environ['TEST_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

# Prevent loading of .env file by setting a flag
os.environ['DOCKER_ENV'] = 'true'

# Set dummy OAuth credentials for testing
os.environ['GITHUB_CLIENT_ID'] = 'test-github-client-id'
os.environ['GITHUB_CLIENT_SECRET'] = 'test-github-client-secret'
os.environ['GOOGLE_CLIENT_ID'] = 'test-google-client-id'
os.environ['GOOGLE_CLIENT_SECRET'] = 'test-google-client-secret'

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

import pytest


@pytest.fixture(scope="session")
def app():
    """Create application for testing."""
    from app import create_app

    app = create_app('testing')
    # Force SQLite for testing (after app creation)
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
        "JWT_SECRET_KEY": "test-secret-key",
        "WTF_CSRF_ENABLED": False,
    })

    from models import db
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
def auth_headers(client, db_session):
    """Create authenticated user and return headers."""
    from models import User
    from werkzeug.security import generate_password_hash
    from flask_jwt_extended import create_access_token
    import uuid

    # Create user directly in database with unique values
    unique_id = str(uuid.uuid4())[:8]
    user = User(
        username=f"testuser_{unique_id}",
        email=f"test_{unique_id}@example.com",
    )
    user.password_hash = generate_password_hash('testpassword123')
    db_session.add(user)
    db_session.commit()

    # Generate token directly using flask_jwt_extended
    access_token = create_access_token(identity=user.id)

    return {"Authorization": f"Bearer {access_token}"}


# =============================================================================
# Factory Fixtures for Idempotent Testing
# =============================================================================

@pytest.fixture
def user_factory(db_session):
    """Factory fixture for creating test users."""
    from models import User
    from werkzeug.security import generate_password_hash
    import uuid

    created_users = []

    def _create_user(**kwargs):
        unique_id = str(uuid.uuid4())[:8]
        defaults = {
            'username': f'testuser_{unique_id}',
            'email': f'test_{unique_id}@example.com',
        }
        defaults.update(kwargs)

        user = User(**defaults)
        user.password_hash = generate_password_hash('password123')
        db_session.add(user)
        db_session.commit()
        created_users.append(user)
        return user

    yield _create_user

    # Cleanup: Delete created users
    for user in created_users:
        db_session.delete(user)
    db_session.commit()


@pytest.fixture
def project_factory(db_session, user_factory):
    """Factory fixture for creating test projects."""
    from models import Project
    import uuid

    created_projects = []

    def _create_project(**kwargs):
        unique_id = str(uuid.uuid4())[:8]
        defaults = {
            'name': f'Test Project {unique_id}',
            'description': 'Test project description',
            'status': 'ACTIVE',
        }
        defaults.update(kwargs)

        if 'owner_id' not in defaults:
            user = user_factory()
            defaults['owner_id'] = user.id

        project = Project(**defaults)
        db_session.add(project)
        db_session.commit()
        created_projects.append(project)
        return project

    yield _create_project

    # Cleanup
    for project in created_projects:
        db_session.delete(project)
    db_session.commit()


@pytest.fixture
def task_factory(db_session, project_factory, user_factory):
    """Factory fixture for creating test tasks."""
    from models import Task
    import uuid

    created_tasks = []
    task_id_counter = [0]  # Use list to make it mutable in closure

    def _create_task(**kwargs):
        task_id_counter[0] += 1
        unique_id = str(uuid.uuid4())[:8]
        defaults = {
            'id': task_id_counter[0],  # Manually set ID for SQLite compatibility
            'title': f'Test Task {unique_id}',
            'content': 'Test task content',
            'status': 'TODO',
            'priority': 'MEDIUM',
        }
        defaults.update(kwargs)

        if 'project_id' not in defaults:
            project = project_factory()
            defaults['project_id'] = project.id

        if 'owner_id' not in defaults:
            user = user_factory()
            defaults['owner_id'] = user.id

        task = Task(**defaults)
        db_session.add(task)
        db_session.commit()
        created_tasks.append(task)
        return task

    yield _create_task

    # Cleanup
    for task in created_tasks:
        db_session.delete(task)
    db_session.commit()


@pytest.fixture
def organization_factory(db_session, user_factory):
    """Factory fixture for creating test organizations."""
    from models import Organization
    import uuid

    created_orgs = []

    def _create_organization(**kwargs):
        unique_id = str(uuid.uuid4())[:8]
        defaults = {
            'name': f'Test Org {unique_id}',
            'slug': f'test-org-{unique_id}',
            'description': 'Test organization',
        }
        defaults.update(kwargs)

        if 'owner_id' not in defaults:
            user = user_factory()
            defaults['owner_id'] = user.id

        org = Organization(**defaults)
        db_session.add(org)
        db_session.commit()
        created_orgs.append(org)
        return org

    yield _create_organization

    # Cleanup
    for org in created_orgs:
        db_session.delete(org)
    db_session.commit()


@pytest.fixture
def agent_factory(db_session, organization_factory, user_factory):
    """Factory fixture for creating test agents."""
    from models import Agent, AgentStatus
    import uuid

    created_agents = []

    def _create_agent(**kwargs):
        unique_id = str(uuid.uuid4())[:8]
        defaults = {
            'name': f'Test Agent {unique_id}',
            'display_name': f'Test Agent {unique_id}',
            'description': 'Test agent',
            'status': AgentStatus.ACTIVE,
        }
        defaults.update(kwargs)

        if 'workspace_id' not in defaults:
            org = organization_factory()
            defaults['workspace_id'] = org.id

        if 'creator_user_id' not in defaults:
            user = user_factory()
            defaults['creator_user_id'] = user.id

        agent = Agent(**defaults)
        db_session.add(agent)
        db_session.commit()
        created_agents.append(agent)
        return agent

    yield _create_agent

    # Cleanup
    for agent in created_agents:
        db_session.delete(agent)
    db_session.commit()
