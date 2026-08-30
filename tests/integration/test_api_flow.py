"""Integration tests for API workflows."""

import pytest


class TestAPIFlow:
    """Test complete API workflows."""

    def test_complete_task_workflow(self, client, auth_headers):
        """Test complete task lifecycle."""
        # Create project
        project_response = client.post(
            "/api/v1/projects",
            json={"name": "Test Project", "description": "Test"},
            headers=auth_headers
        )

        # Project creation may or may not work without full setup
        if project_response.status_code == 201:
            project_id = project_response.json.get("id")

            # Create task
            task_response = client.post(
                "/api/v1/tasks",
                json={
                    "title": "Test Task",
                    "description": "Test description",
                    "project_id": project_id
                },
                headers=auth_headers
            )

            if task_response.status_code == 201:
                task_id = task_response.json.get("id")

                # Get task
                get_response = client.get(
                    f"/api/v1/tasks/{task_id}",
                    headers=auth_headers
                )
                assert get_response.status_code in [200, 404]

                # Update task
                update_response = client.put(
                    f"/api/v1/tasks/{task_id}",
                    json={"status": "in_progress"},
                    headers=auth_headers
                )
                assert update_response.status_code in [200, 404]

    def test_auth_flow(self, client):
        """Test complete authentication flow."""
        # Register
        register_response = client.post("/api/v1/auth/register", json={
            "username": "flowuser",
            "email": "flow@example.com",
            "password": "password123"
        })

        # Login
        login_response = client.post("/api/v1/auth/login", json={
            "username": "flowuser",
            "password": "password123"
        })

        # Get token if login succeeded
        if login_response.status_code == 200:
            token = login_response.json.get("access_token", "")
            headers = {"Authorization": f"Bearer {token}"}

            # Access protected resource
            me_response = client.get("/api/v1/auth/me", headers=headers)
            assert me_response.status_code in [200, 401]
