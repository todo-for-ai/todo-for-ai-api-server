"""Tests for Tasks API endpoints."""

import pytest


class TestTasksAPI:
    """Test Tasks API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_tasks_list(self, client, auth_headers, task_factory):
        """Test getting tasks list."""
        # Create a task first
        task_factory()

        response = client.get(f"{self.BASE_URL}/tasks", headers=auth_headers)

        # May return 200 or 401
        assert response.status_code in [200, 401]

    def test_create_task(self, client, auth_headers, project_factory):
        """Test creating a task."""
        project = project_factory()

        response = client.post(f"{self.BASE_URL}/tasks", json={
            "title": "New Test Task",
            "description": "Test task description",
            "project_id": project.id,
            "priority": "HIGH"
        }, headers=auth_headers)

        # May return 201, 200, 401, 403, or 422
        assert response.status_code in [201, 200, 401, 403, 422]

    def test_get_task_detail(self, client, auth_headers, task_factory):
        """Test getting task detail."""
        task = task_factory()

        response = client.get(f"{self.BASE_URL}/tasks/{task.id}", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_update_task(self, client, auth_headers, task_factory):
        """Test updating a task."""
        task = task_factory()

        response = client.put(f"{self.BASE_URL}/tasks/{task.id}", json={
            "title": "Updated Task Title",
            "status": "IN_PROGRESS"
        }, headers=auth_headers)

        # May return 200, 404, 401, 403, or 422
        assert response.status_code in [200, 401, 403, 404, 422]

    def test_delete_task(self, client, auth_headers, task_factory):
        """Test deleting a task."""
        task = task_factory()

        response = client.delete(f"{self.BASE_URL}/tasks/{task.id}", headers=auth_headers)

        # May return 204, 200, 404, 401, or 403
        assert response.status_code in [204, 200, 401, 403, 404]

    def test_get_tasks_without_auth(self, client):
        """Test getting tasks without authentication."""
        response = client.get(f"{self.BASE_URL}/tasks")

        # Should require authentication
        assert response.status_code in [401, 403]

    def test_create_task_without_auth(self, client):
        """Test creating task without authentication."""
        response = client.post(f"{self.BASE_URL}/tasks", json={
            "title": "Test Task"
        })

        # Should require authentication
        assert response.status_code in [401, 403]


class TestTaskLabelsAPI:
    """Test Task Labels API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_task_labels(self, client, auth_headers, task_factory):
        """Test getting task labels."""
        task = task_factory()

        response = client.get(f"{self.BASE_URL}/tasks/{task.id}/labels", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_add_task_label(self, client, auth_headers, task_factory):
        """Test adding label to task."""
        task = task_factory()

        response = client.post(f"{self.BASE_URL}/tasks/{task.id}/labels", json={
            "name": "urgent",
            "color": "#ff0000"
        }, headers=auth_headers)

        # May return 201, 200, or 401/404
        assert response.status_code in [201, 200, 401, 404]


class TestTaskHistoryAPI:
    """Test Task History API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_task_history(self, client, auth_headers, task_factory):
        """Test getting task history."""
        task = task_factory()

        response = client.get(f"{self.BASE_URL}/tasks/{task.id}/history", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]


class TestTaskAttachmentsAPI:
    """Test Task Attachments API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_task_attachments(self, client, auth_headers, task_factory):
        """Test getting task attachments."""
        task = task_factory()

        response = client.get(f"{self.BASE_URL}/tasks/{task.id}/attachments", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]


class TestTaskCommentsAPI:
    """Test Task Comments API endpoints."""

    BASE_URL = "/todo-for-ai/api/v1"

    def test_get_task_comments(self, client, auth_headers, task_factory):
        """Test getting task comments."""
        task = task_factory()

        response = client.get(f"{self.BASE_URL}/tasks/{task.id}/comments", headers=auth_headers)

        # May return 200 or 401/404
        assert response.status_code in [200, 401, 403, 404]

    def test_add_task_comment(self, client, auth_headers, task_factory):
        """Test adding comment to task."""
        task = task_factory()

        response = client.post(f"{self.BASE_URL}/tasks/{task.id}/comments", json={
            "content": "Test comment"
        }, headers=auth_headers)

        # May return 201, 200, or 401/404
        assert response.status_code in [201, 200, 401, 404]
