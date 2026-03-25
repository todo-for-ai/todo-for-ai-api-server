"""Tests for authentication core."""

import pytest
from werkzeug.security import generate_password_hash, check_password_hash


class TestAuthCore:
    """Test authentication utilities."""

    def test_password_hashing(self):
        """Test password is properly hashed."""
        password = "testpassword123"
        hashed = generate_password_hash(password)

        assert hashed != password
        assert hashed.startswith("pbkdf2:sha256:")
        assert check_password_hash(hashed, password) is True
        assert check_password_hash(hashed, "wrongpassword") is False


class TestPasswordVerification:
    """Test password verification edge cases."""

    def test_password_hashing_different_passwords(self):
        """Test different passwords produce different hashes."""
        password1 = "password123"
        password2 = "password124"

        hash1 = generate_password_hash(password1)
        hash2 = generate_password_hash(password2)

        assert hash1 != hash2
        assert check_password_hash(hash1, password1) is True
        assert check_password_hash(hash2, password2) is True
        assert check_password_hash(hash1, password2) is False

    def test_password_hashing_same_password_different_salt(self):
        """Test same password produces different hashes due to salt."""
        password = "mypassword"

        hash1 = generate_password_hash(password)
        hash2 = generate_password_hash(password)

        # Same password should produce different hashes (due to salt)
        assert hash1 != hash2
        # But both should verify correctly
        assert check_password_hash(hash1, password) is True
        assert check_password_hash(hash2, password) is True

    def test_empty_password(self):
        """Test empty password handling."""
        password = ""
        hashed = generate_password_hash(password)

        assert hashed != password
        assert check_password_hash(hashed, password) is True
        assert check_password_hash(hashed, "notempty") is False

    def test_long_password(self):
        """Test long password handling."""
        password = "a" * 1000
        hashed = generate_password_hash(password)

        assert hashed != password
        assert check_password_hash(hashed, password) is True
        assert check_password_hash(hashed, password[:-1]) is False
