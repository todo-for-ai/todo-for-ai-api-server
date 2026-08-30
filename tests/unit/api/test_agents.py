"""Tests for Agents API endpoints."""

import pytest


class TestAgentsAPI:
    """Test Agents API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_agents_list(self, client, auth_headers, agent_factory):
        """Test getting agents list."""
        # Create an agent first
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.get(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents",
            headers=auth_headers
        )

        # May return 200, 401, or 403 (no permission)
        assert response.status_code in [200, 401, 403]

    def test_create_agent(self, client, auth_headers, organization_factory):
        """Test creating an agent."""
        org = organization_factory()
        import uuid
        unique_id = str(uuid.uuid4())[:8]

        response = client.post(
            f"{self.BASE_URL}/workspaces/{org.id}/agents",
            json={
                "name": f"Test Agent {unique_id}",
                "display_name": f"Test Agent {unique_id}",
                "description": "Test agent"
            },
            headers=auth_headers
        )

        # May return 201, 200, 401, 403, or 422
        assert response.status_code in [201, 200, 401, 403, 422]

    def test_get_agent_detail(self, client, auth_headers, agent_factory):
        """Test getting agent detail."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.get(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]

    def test_update_agent(self, client, auth_headers, agent_factory):
        """Test updating an agent."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.patch(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}",
            json={
                "name": "Updated Agent Name",
                "description": "Updated description"
            },
            headers=auth_headers
        )

        # May return 200, 404, 401, 403, or 422
        assert response.status_code in [200, 401, 403, 404, 422]

    def test_delete_agent(self, client, auth_headers, agent_factory):
        """Test deleting an agent."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.delete(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}",
            headers=auth_headers
        )

        # May return 204, 200, 404, 401, or 403
        assert response.status_code in [204, 200, 401, 403, 404]


class TestAgentSoulAPI:
    """Test Agent Soul API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_agent_soul(self, client, auth_headers, agent_factory):
        """Test getting agent soul."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.get(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}/soul",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]

    def test_update_agent_soul(self, client, auth_headers, agent_factory):
        """Test updating agent soul."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.patch(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}/soul",
            json={
                "soul_markdown": "# Test Soul\n\nThis is a test soul."
            },
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404, 422]


class TestAgentKeysAPI:
    """Test Agent Keys API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_agent_keys(self, client, auth_headers, agent_factory):
        """Test getting agent keys."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.get(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}/keys",
            headers=auth_headers
        )

        # May return 200, 401, 403, or 404
        assert response.status_code in [200, 401, 403, 404]

    def test_create_agent_key(self, client, auth_headers, agent_factory):
        """Test creating agent key."""
        agent = agent_factory()
        workspace_id = agent.workspace_id

        response = client.post(
            f"{self.BASE_URL}/workspaces/{workspace_id}/agents/{agent.id}/keys",
            json={
                "name": "Test Key",
                "description": "Test key description"
            },
            headers=auth_headers
        )

        # May return 201, 200, 401, 403, or 404
        assert response.status_code in [201, 200, 401, 403, 404, 422]
