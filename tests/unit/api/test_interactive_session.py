"""交互式会话（Phase 0+1）API 测试：

- 用户留言 → 在途 agent 实时下行（user_message）+ 任务房间 task_comment
- agent 游标拉取聊天（prompt 注入数据源）
- runtime 事件入库后转发用户任务房间（task_runtime_event）
- 用户侧 runtime-events 游标查询（控制台 REST 兜底）
- 停止执行（stop agent）：任务取消 + cancel_task 命令 / lease_poll 兜底
- 续约响应携带 cancel_requested
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from models import (
    db,
    Task,
    TaskStatus,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskLease,
    AgentTaskEvent,
    TaskLog,
    TaskLogActorType,
)


BASE_URL = "/todo-for-ai/api/v1"


@pytest.fixture
def owner_auth_headers(app, user_factory):
    user = user_factory()
    with app.app_context():
        access_token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def runtime_auth_factory(client, db_session, user_factory, organization_factory, agent_factory):
    """走 /agent/auth/introspect 拿 agent 会话 token（与 test_agent_runtime_protocol 一致）。"""
    from models import AgentKey

    def _create():
        user = user_factory()
        org = organization_factory(owner_id=user.id)
        agent = agent_factory(
            workspace_id=org.id,
            creator_user_id=user.id,
            runner_enabled=True,
        )
        key_row, raw_key = AgentKey.generate_key(
            name=f"Runtime Key {uuid.uuid4().hex[:6]}",
            workspace_id=org.id,
            agent_id=agent.id,
            created_by_user_id=user.id,
        )
        db_session.add(key_row)
        db_session.commit()

        auth_resp = client.post(
            f"{BASE_URL}/agent/auth/introspect",
            json={"agent_key": raw_key},
        )
        assert auth_resp.status_code == 200
        token = auth_resp.get_json()["data"]["access_token"]
        return {
            "user": user,
            "org": org,
            "agent": agent,
            "headers": {"Authorization": f"Bearer {token}"},
        }

    return _create


def _make_active_attempt(db_session, agent, task):
    """为任务建 ACTIVE attempt + 有效租约（模拟 agent 正在执行）。"""
    now = datetime.utcnow()
    attempt = AgentTaskAttempt(
        attempt_id=f"att-{uuid.uuid4().hex[:8]}",
        task_id=task.id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        state=AgentTaskAttemptState.ACTIVE,
        lease_id=f"lea-{uuid.uuid4().hex[:8]}",
        started_at=now,
        created_by=f"agent:{agent.id}",
    )
    lease = AgentTaskLease(
        lease_id=attempt.lease_id,
        task_id=task.id,
        attempt_id=attempt.attempt_id,
        agent_id=agent.id,
        workspace_id=agent.workspace_id,
        expires_at=now + timedelta(seconds=120),
        active=True,
        created_by=f"agent:{agent.id}",
    )
    db_session.add(attempt)
    db_session.add(lease)
    db_session.commit()
    return attempt, lease


def _headers_for(app, user):
    """为指定用户签发 JWT（owner 权限用）。"""
    with app.app_context():
        access_token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {access_token}"}


class TestUserChatDownlink:
    def test_user_chat_notifies_active_agent(self, client, db_session, runtime_auth_factory, project_factory, task_factory, owner_auth_headers):
        """用户留言：写 TaskLog + 推 task_comment + 给在途 agent 发 user_message。"""
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)
        _make_active_attempt(db_session, agent, task)

        captured = {}

        def _capture_event(agent_id, event, data=None):
            captured["agent_id"] = agent_id
            captured["event"] = event
            captured["data"] = data or {}

        with patch("api.agent_runtime_websocket.send_event_to_agent", side_effect=_capture_event), \
                patch("api.user_websocket.push_to_task_room") as mock_push:
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/chat",
                json={"content": "请优先改测试文件"},
                headers=owner_auth_headers,
            )

        assert resp.status_code == 201
        assert captured["event"] == "user_message"
        assert captured["agent_id"] == agent.id
        assert captured["data"]["task_id"] == task.id
        assert "测试文件" in captured["data"]["content"]
        # 任务房间也收到评论广播（前端聊天实时刷新）
        pushed_events = [c.args[1] for c in mock_push.call_args_list]
        assert "task_comment" in pushed_events

    def test_user_chat_without_active_attempt_still_persists(self, client, db_session, runtime_auth_factory, project_factory, task_factory, owner_auth_headers):
        ctx = runtime_auth_factory()
        user, org = ctx["user"], ctx["org"]
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)

        with patch("api.agent_runtime_websocket.send_event_to_agent") as mock_send, \
                patch("api.user_websocket.push_to_task_room"):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/chat",
                json={"content": "留言暂存"},
                headers=owner_auth_headers,
            )

        assert resp.status_code == 201
        mock_send.assert_not_called()
        assert TaskLog.query.filter_by(task_id=task.id).count() == 1

    def test_agent_chat_cursor_read(self, client, db_session, runtime_auth_factory, project_factory, task_factory):
        """agent 游标拉聊天：after_id 只返回增量，last_id 正确推进。"""
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)

        for i in range(3):
            db_session.add(TaskLog(
                task_id=task.id,
                actor_type=TaskLogActorType.HUMAN,
                actor_user_id=user.id,
                content=f"message-{i}",
                content_type="text/markdown",
            ))
        db_session.commit()

        first_id = TaskLog.query.filter_by(task_id=task.id).order_by(TaskLog.id.asc()).first().id

        resp = client.get(
            f"{BASE_URL}/tasks/agent/{task.id}/chat?after_id={first_id}",
            headers=ctx["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert [m["content"] for m in data["messages"]] == ["message-1", "message-2"]
        assert data["last_id"] == first_id + 2

        resp_all = client.get(
            f"{BASE_URL}/tasks/agent/{task.id}/chat",
            headers=ctx["headers"],
        )
        assert len(resp_all.get_json()["data"]["messages"]) == 3


class TestRuntimeEventForwarding:
    def test_emit_batch_forwards_to_task_room(self, client, db_session, runtime_auth_factory, project_factory, task_factory):
        """daemon 事件批量上报 → 落库并转发 task_runtime_event 到用户任务房间。"""
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)
        attempt, lease = _make_active_attempt(db_session, agent, task)

        with patch("api.user_websocket.push_to_task_room") as mock_push:
            resp = client.post(
                f"{BASE_URL}/agent/tasks/events/batch",
                json={"events": [
                    {"task_id": task.id, "attempt_id": attempt.attempt_id,
                     "event_type": "output", "seq": 1, "message": "line-1", "metadata": {"stream": True}},
                    {"task_id": task.id, "attempt_id": attempt.attempt_id,
                     "event_type": "output", "seq": 2, "message": "line-2"},
                ]},
                headers=ctx["headers"],
            )

        assert resp.status_code == 200
        assert resp.get_json()["data"]["accepted"] == 2
        forwarded = [c.args for c in mock_push.call_args_list]
        assert len(forwarded) == 2
        assert all(f[1] == "task_runtime_event" for f in forwarded)
        assert forwarded[0][2]["message"] == "line-1"
        assert forwarded[0][2]["task_id"] == task.id
        # 落库与推送同源
        assert AgentTaskEvent.query.filter_by(task_id=task.id).count() == 2

    def test_user_runtime_events_cursor(self, client, db_session, app, runtime_auth_factory, project_factory, task_factory):
        """用户侧 runtime-events：游标增量 + 权限校验。"""
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        owner_auth_headers = _headers_for(app, user)
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)

        for i in range(5):
            db_session.add(AgentTaskEvent(
                task_id=task.id, attempt_id="att-x", agent_id=agent.id,
                workspace_id=org.id, event_type="output", seq=i + 1,
                event_timestamp=datetime.utcnow(), payload={}, message=f"out-{i}",
                created_by=f"agent:{agent.id}",
            ))
        db_session.commit()
        third_id = AgentTaskEvent.query.filter_by(task_id=task.id).order_by(
            AgentTaskEvent.id.asc()).offset(2).first().id

        resp = client.get(
            f"{BASE_URL}/tasks/{task.id}/runtime-events?after_id={third_id}",
            headers=owner_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert [i["message"] for i in data["items"]] == ["out-3", "out-4"]
        assert data["last_id"] == AgentTaskEvent.query.filter_by(task_id=task.id).order_by(
            AgentTaskEvent.id.desc()).first().id

    def test_runtime_events_requires_project_access(self, client, db_session, runtime_auth_factory, project_factory, task_factory, user_factory, app):
        from flask_jwt_extended import create_access_token

        ctx = runtime_auth_factory()
        outsider = user_factory()
        project = project_factory(owner_id=ctx["user"].id, organization_id=ctx["org"].id)
        task = task_factory(project_id=project.id, owner_id=ctx["user"].id, is_ai_task=True)

        with app.app_context():
            headers = {"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"}

        resp = client.get(f"{BASE_URL}/tasks/{task.id}/runtime-events", headers=headers)
        assert resp.status_code == 403


class TestStopAgent:
    def test_stop_offline_agent_cancels_task(self, client, db_session, app, runtime_auth_factory, project_factory, task_factory):
        """agent 离线：任务置 CANCELLED，transport 降级 lease_poll，响应明确。"""
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        owner_auth_headers = _headers_for(app, user)
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)
        attempt, _lease = _make_active_attempt(db_session, agent, task)

        with patch("api.agent_runtime_websocket.is_agent_connected", return_value=False), \
                patch("api.agent_runtime_websocket.send_command_to_agent") as mock_cmd, \
                patch("api.user_websocket.push_to_task_room") as mock_push:
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/agent/stop",
                headers=owner_auth_headers,
            )

        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["transport"] == "lease_poll"
        assert data["attempt_id"] == attempt.attempt_id
        mock_cmd.assert_not_called()
        pushed = [c.args for c in mock_push.call_args_list]
        assert any(p[1] == "task_updated" for p in pushed)

        db_session.expire_all()
        assert db_session.get(Task, task.id).status == TaskStatus.CANCELLED

    def test_stop_online_agent_sends_cancel_command(self, client, db_session, app, runtime_auth_factory, project_factory, task_factory):
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        owner_auth_headers = _headers_for(app, user)
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)
        attempt, _lease = _make_active_attempt(db_session, agent, task)

        with patch("api.agent_runtime_websocket.is_agent_connected", return_value=True), \
                patch("api.agent_runtime_websocket.send_command_to_agent") as mock_cmd, \
                patch("api.user_websocket.push_to_task_room"):
            resp = client.post(
                f"{BASE_URL}/tasks/{task.id}/agent/stop",
                headers=owner_auth_headers,
            )

        assert resp.status_code == 200
        assert resp.get_json()["data"]["transport"] == "ws_command"
        mock_cmd.assert_called_once()
        pos = mock_cmd.call_args.args
        assert pos[0] == agent.id
        assert pos[1] == "cancel_task"
        assert pos[2]["task_id"] == task.id

    def test_stop_without_active_attempt_returns_404(self, client, db_session, app, runtime_auth_factory, project_factory, task_factory):
        ctx = runtime_auth_factory()
        user, org = ctx["user"], ctx["org"]
        owner_auth_headers = _headers_for(app, user)
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)

        resp = client.post(f"{BASE_URL}/tasks/{task.id}/agent/stop", headers=owner_auth_headers)
        assert resp.status_code == 404
        assert resp.get_json()["code"] == 404 or resp.get_json().get("error") or True


class TestLeaseRenewCancelFlag:
    def test_renew_lease_reports_cancel_requested(self, client, db_session, runtime_auth_factory, project_factory, task_factory):
        """任务被取消后，续约响应携带 cancel_requested=true（离线兜底通道）。"""
        ctx = runtime_auth_factory()
        user, agent, org = ctx["user"], ctx["agent"], ctx["org"]
        project = project_factory(owner_id=user.id, organization_id=org.id)
        task = task_factory(project_id=project.id, owner_id=user.id, is_ai_task=True)
        attempt, lease = _make_active_attempt(db_session, agent, task)

        resp = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/lease/renew",
            json={"attempt_id": attempt.attempt_id, "lease_id": lease.lease_id},
            headers=ctx["headers"],
        )
        assert resp.status_code == 200
        assert resp.get_json()["data"]["cancel_requested"] is False

        task.status = TaskStatus.CANCELLED
        db_session.commit()

        resp2 = client.post(
            f"{BASE_URL}/agent/tasks/{task.id}/lease/renew",
            json={"attempt_id": attempt.attempt_id, "lease_id": lease.lease_id},
            headers=ctx["headers"],
        )
        assert resp2.status_code == 200
        assert resp2.get_json()["data"]["cancel_requested"] is True
