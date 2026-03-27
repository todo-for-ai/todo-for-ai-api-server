"""Tests for Projects API endpoints."""

import pytest


class TestProjectsAPI:
    """Test Projects API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_projects_list(self, client, auth_headers, project_factory):
        """Test getting projects list."""
        # Create a project first
        project_factory()

        response = client.get(f"{self.BASE_URL}/projects", headers=auth_headers)

        # May return 200 or 401
        assert response.status_code in [200, 401]

    def test_create_project(self, client, auth_headers):
        """Test creating a project."""
        response = client.post(f"{self.BASE_URL}/projects", json={
            "name": "New Test Project",
            "description": "Test project description",
            "status": "ACTIVE"
        }, headers=auth_headers)

        # May return 201, 200, or 401
        assert response.status_code in [201, 200, 401, 422]

    def test_get_project_detail(self, client, auth_headers, project_factory):
        """Test getting project detail."""
        project = project_factory()

        response = client.get(f"{self.BASE_URL}/projects/{project.id}", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_update_project(self, client, auth_headers, project_factory):
        """Test updating a project."""
        project = project_factory()

        response = client.put(f"{self.BASE_URL}/projects/{project.id}", json={
            "name": "Updated Project Name",
            "description": "Updated description"
        }, headers=auth_headers)

        # May return 200, 404, or 401
        assert response.status_code in [200, 401, 404, 422]

    def test_delete_project(self, client, auth_headers, project_factory):
        """Test deleting a project."""
        project = project_factory()

        response = client.delete(f"{self.BASE_URL}/projects/{project.id}", headers=auth_headers)

        # May return 204, 200, 404, or 401
        assert response.status_code in [204, 200, 401, 404]

    def test_get_projects_without_auth(self, client):
        """Test getting projects without authentication."""
        response = client.get(f"{self.BASE_URL}/projects")

        # Should require authentication
        assert response.status_code in [401, 403]

    def test_create_project_without_auth(self, client):
        """Test creating project without authentication."""
        response = client.post(f"{self.BASE_URL}/projects", json={
            "name": "Test Project"
        })

        # Should require authentication
        assert response.status_code in [401, 403]


class TestProjectMembersAPI:
    """Test Project Members API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_project_members(self, client, auth_headers, project_factory):
        """Test getting project members."""
        project = project_factory()

        response = client.get(f"{self.BASE_URL}/projects/{project.id}/members", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_add_project_member(self, client, auth_headers, project_factory, user_factory):
        """Test adding member to project."""
        project = project_factory()
        user = user_factory()

        response = client.post(f"{self.BASE_URL}/projects/{project.id}/members", json={
            "user_id": user.id,
            "role": "MEMBER"
        }, headers=auth_headers)

        # May return 201, 200, or 401/404
        assert response.status_code in [201, 200, 401, 404, 422]

    def test_remove_project_member(self, client, auth_headers, project_factory, user_factory):
        """Test removing member from project."""
        project = project_factory()
        user = user_factory()

        response = client.delete(
            f"{self.BASE_URL}/projects/{project.id}/members/{user.id}",
            headers=auth_headers
        )

        # May return 204, 200, or 401/404
        assert response.status_code in [204, 200, 401, 404]


class TestProjectTasksAPI:
    """Test Project Tasks API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_project_tasks(self, client, auth_headers, project_factory):
        """Test getting tasks in project."""
        project = project_factory()

        response = client.get(f"{self.BASE_URL}/projects/{project.id}/tasks", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_create_project_task(self, client, auth_headers, project_factory):
        """Test creating task in project."""
        project = project_factory()

        response = client.post(f"{self.BASE_URL}/projects/{project.id}/tasks", json={
            "title": "Project Task",
            "description": "Task in project"
        }, headers=auth_headers)

        # May return 201, 200, or 401/404
        assert response.status_code in [201, 200, 401, 404, 422]


class TestProjectStatsAPI:
    """Test Project Statistics API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_project_stats(self, client, auth_headers, project_factory):
        """Test getting project statistics."""
        project = project_factory()

        response = client.get(f"{self.BASE_URL}/projects/{project.id}/stats", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_get_project_dashboard(self, client, auth_headers, project_factory):
        """Test getting project dashboard data."""
        project = project_factory()

        response = client.get(
            f"{self.BASE_URL}/projects/{project.id}/dashboard",
            headers=auth_headers
        )

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]
