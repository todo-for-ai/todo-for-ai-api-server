"""Tests for User model."""

import pytest
from werkzeug.security import generate_password_hash, check_password_hash


class TestUserModel:
    """Test User model."""

    def test_create_user(self, db_session):
        """Test creating user."""
        from models import User

        user = User(
            username="testuser",
            email="test@example.com"
        )
        user.password_hash = generate_password_hash("password123")
        db_session.add(user)
        db_session.commit()

        assert user.id is not None
        assert user.username == "testuser"
        assert user.email == "test@example.com"

    def test_user_password_hashing(self, db_session):
        """Test password is properly hashed."""
        from models import User

        user = User(username="testuser2", email="test2@example.com")
        user.password_hash = generate_password_hash("password123")

        assert user.password_hash != "password123"
        assert check_password_hash(user.password_hash, "password123") is True
        assert check_password_hash(user.password_hash, "wrongpassword") is False

    def test_user_repr(self, db_session):
        """Test user string representation."""
        from models import User

        user = User(username="testuser3", email="test3@example.com")
        db_session.add(user)
        db_session.commit()

        assert "testuser3" in repr(user)
