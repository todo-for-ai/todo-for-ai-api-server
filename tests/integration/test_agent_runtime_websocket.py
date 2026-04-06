"""Integration tests for Agent Runtime WebSocket namespace."""

import pytest


class TestAgentRuntimeWebSocket:
    """Test Agent Runtime WebSocket namespace /agent/ws."""

    @pytest.fixture
    def agent_with_key(self, db_session, agent_factory, user_factory):
        """Create an agent with a valid key."""
        from models import AgentKey

        user = user_factory()
        agent = agent_factory(
            workspace_id=user.id,
            creator_user_id=user.id,
            runner_enabled=True,
        )
        key_row, raw_key = AgentKey.generate_key(
            name="Test Key",
            workspace_id=agent.workspace_id,
            agent_id=agent.id,
            created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()
        return agent, raw_key

    def test_websocket_auth_success(self, app, agent_with_key):
        """Test WebSocket connection with valid agent key."""
        from app import socketio

        agent, raw_key = agent_with_key

        with app.app_context():
            client = socketio.test_client(
                app,
                namespace='/agent/ws',
                auth={'agent_key': raw_key},
            )
            assert client.is_connected('/agent/ws')

            received = client.get_received('/agent/ws')
            events = {msg['name']: msg['args'][0] for msg in received}

            assert 'auth_success' in events
            assert events['auth_success']['agent_id'] == agent.id
            assert events['auth_success']['workspace_id'] == agent.workspace_id

            if client.is_connected('/agent/ws'):
                client.disconnect(namespace='/agent/ws')

    def test_websocket_auth_failure(self, app):
        """Test WebSocket connection with invalid key is rejected."""
        from app import socketio

        with app.app_context():
            client = socketio.test_client(
                app,
                namespace='/agent/ws',
                auth={'agent_key': 'invalid_key'},
            )
            # Auth failure should reject namespace connection
            assert not client.is_connected('/agent/ws')
            if client.is_connected('/agent/ws'):
                client.disconnect(namespace='/agent/ws')

    def test_heartbeat_ack(self, app, agent_with_key):
        """Test heartbeat is acknowledged."""
        from app import socketio

        agent, raw_key = agent_with_key

        with app.app_context():
            client = socketio.test_client(
                app,
                namespace='/agent/ws',
                auth={'agent_key': raw_key},
            )
            client.emit('heartbeat', {'timestamp': 1234567890}, namespace='/agent/ws')

            received = client.get_received('/agent/ws')
            event_names = [msg['name'] for msg in received]

            assert 'heartbeat_ack' in event_names
            if client.is_connected('/agent/ws'):
                client.disconnect(namespace='/agent/ws')

    def test_task_push_via_websocket(self, app, db_session, agent_with_key):
        """Test pushing a task to a connected agent via WebSocket."""
        from app import socketio
        from api.agent_runtime_websocket import push_task_to_agent

        agent, raw_key = agent_with_key

        with app.app_context():
            ws_client = socketio.test_client(
                app,
                namespace='/agent/ws',
                auth={'agent_key': raw_key},
            )
            assert ws_client.is_connected('/agent/ws')

            task_data = {
                'task_id': 999,
                'attempt_id': 'att_test123',
                'lease_id': 'lea_test123',
                'project_id': 1,
                'title': 'WebSocket Push Test Task',
                'payload': {'prompt': 'test push'},
                'priority': 'HIGH',
                'workspace_id': agent.workspace_id,
            }
            push_task_to_agent(agent.id, task_data)

            received = ws_client.get_received('/agent/ws')
            event_names = [msg['name'] for msg in received]

            assert 'task_assign' in event_names

            task_assign = next(
                (msg['args'][0] for msg in received if msg['name'] == 'task_assign'),
                None
            )
            assert task_assign is not None
            assert task_assign['task_id'] == 999
            assert task_assign['attempt_id'] == 'att_test123'
            assert task_assign['lease_id'] == 'lea_test123'

            if ws_client.is_connected('/agent/ws'):
                ws_client.disconnect(namespace='/agent/ws')
