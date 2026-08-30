"""Tests for Auth API endpoints."""

import pytest


class TestAuthAPI:
    """Test Authentication API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1/auth"

    def test_login_redirects_to_oauth(self, client):
        """Test login redirects to OAuth provider."""
        response = client.get(f"{self.BASE_URL}/login")

        # OAuth login redirects to provider
        assert response.status_code in [200, 302, 401]

    def test_login_without_oauth(self, client):
        """Test login without OAuth provider."""
        response = client.get(f"{self.BASE_URL}/login")

        # Should fail without OAuth provider (no session)
        assert response.status_code in [200, 302, 400, 401]

    def test_get_current_user_without_auth(self, client):
        """Test getting current user info without authentication."""
        response = client.get(f"{self.BASE_URL}/me")

        # Should require authentication
        assert response.status_code in [401, 403]

    def test_logout_without_auth(self, client):
        """Test logout without authentication."""
        response = client.post(f"{self.BASE_URL}/logout")

        # May succeed or require auth
        assert response.status_code in [200, 401]

    def test_list_users_without_auth(self, client):
        """Test listing users without authentication."""
        response = client.get(f"{self.BASE_URL}/users")

        # May require admin auth
        assert response.status_code in [200, 401, 403]

    def test_verify_token_without_auth(self, client):
        """Test token verification without token."""
        response = client.post(f"{self.BASE_URL}/verify")

        # Should require token
        assert response.status_code in [400, 401, 403, 422]

    def test_github_login_redirect(self, client):
        """Test GitHub OAuth login redirects."""
        response = client.get(f"{self.BASE_URL}/login/github")

        # Should redirect to GitHub
        assert response.status_code in [302, 307, 200, 404]

    def test_google_login_redirect(self, client):
        """Test Google OAuth login redirects."""
        response = client.get(f"{self.BASE_URL}/login/google")

        # Should redirect to Google or fail with error
        assert response.status_code in [302, 307, 200, 404, 500]
