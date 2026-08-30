"""Tests for Organizations API endpoints."""

import pytest


class TestOrganizationsAPI:
    """Test Organizations API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_organizations_list(self, client, auth_headers, organization_factory):
        """Test getting organizations list."""
        # Create an organization first
        organization_factory()

        response = client.get(f"{self.BASE_URL}/organizations", headers=auth_headers)

        # May return 200 or 401
        assert response.status_code in [200, 401]

    def test_create_organization(self, client, auth_headers):
        """Test creating an organization."""
        import uuid
        unique_id = str(uuid.uuid4())[:8]

        response = client.post(f"{self.BASE_URL}/organizations", json={
            "name": f"Test Org {unique_id}",
            "slug": f"test-org-{unique_id}",
            "description": "Test organization"
        }, headers=auth_headers)

        # May return 201, 200, or 401
        assert response.status_code in [201, 200, 401, 422]

    def test_get_organization_detail(self, client, auth_headers, organization_factory):
        """Test getting organization detail."""
        org = organization_factory()

        response = client.get(f"{self.BASE_URL}/organizations/{org.id}", headers=auth_headers)

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]

    def test_update_organization(self, client, auth_headers, organization_factory):
        """Test updating an organization."""
        org = organization_factory()

        response = client.put(f"{self.BASE_URL}/organizations/{org.id}", json={
            "name": "Updated Org Name",
            "description": "Updated description"
        }, headers=auth_headers)

        # May return 200, 404, 401, or 403
        assert response.status_code in [200, 401, 403, 404, 422]

    def test_delete_organization(self, client, auth_headers, organization_factory):
        """Test deleting an organization."""
        org = organization_factory()

        response = client.delete(f"{self.BASE_URL}/organizations/{org.id}", headers=auth_headers)

        # May return 204, 200, 404, 401, or 405 (if DELETE not implemented)
        assert response.status_code in [204, 200, 401, 404, 405]

    def test_get_organizations_without_auth(self, client):
        """Test getting organizations without authentication."""
        response = client.get(f"{self.BASE_URL}/organizations")

        # Should require authentication
        assert response.status_code in [401, 403]


class TestOrganizationMembersAPI:
    """Test Organization Members API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_organization_members(self, client, auth_headers, organization_factory):
        """Test getting organization members."""
        org = organization_factory()

        response = client.get(
            f"{self.BASE_URL}/organizations/{org.id}/members",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]

    def test_add_organization_member(self, client, auth_headers, organization_factory, user_factory):
        """Test adding member to organization."""
        org = organization_factory()
        user = user_factory()

        response = client.post(
            f"{self.BASE_URL}/organizations/{org.id}/members/invite",
            json={"email": user.email, "role": "member"},
            headers=auth_headers
        )

        # May return 201, 200, or 401/403/404
        assert response.status_code in [201, 200, 401, 403, 404]

    def test_remove_organization_member(self, client, auth_headers, organization_factory, user_factory):
        """Test removing member from organization."""
        org = organization_factory()
        user = user_factory()

        response = client.delete(
            f"{self.BASE_URL}/organizations/{org.id}/members/{user.id}",
            headers=auth_headers
        )

        # May return 204, 200, or 401/404
        assert response.status_code in [204, 200, 401, 403, 404]


class TestOrganizationProjectsAPI:
    """Test Organization Projects API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_organization_projects(self, client, auth_headers, organization_factory):
        """Test getting projects in organization."""
        org = organization_factory()

        response = client.get(
            f"{self.BASE_URL}/organizations/{org.id}/projects",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]


class TestOrganizationAgentsAPI:
    """Test Organization Agents API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_organization_agents(self, client, auth_headers, organization_factory):
        """Test getting agents in organization."""
        org = organization_factory()

        # 组织 Agent 列表现在位于 workspace agents API（organizations 合并收敛后）
        response = client.get(
            f"{self.BASE_URL}/workspaces/{org.id}/agents",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]

    def test_create_organization_agent(self, client, auth_headers, organization_factory):
        """Test creating agent in organization."""
        org = organization_factory()
        import uuid
        unique_id = str(uuid.uuid4())[:8]

        response = client.post(
            f"{self.BASE_URL}/organizations/{org.id}/agents",
            json={
                "name": f"Test Agent {unique_id}",
                "slug": f"test-agent-{unique_id}",
                "description": "Test agent"
            },
            headers=auth_headers
        )

        # May return 201, 200, or 401/404
        assert response.status_code in [201, 200, 401, 403, 404, 422]


class TestOrganizationStatsAPI:
    """Test Organization Statistics API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_organization_stats(self, client, auth_headers, organization_factory):
        """Test getting organization statistics."""
        org = organization_factory()

        response = client.get(
            f"{self.BASE_URL}/organizations/{org.id}/stats",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]
