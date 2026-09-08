"""Agent 工作区洞察 API（api/agent_workspace_insights 包）单元回归。

覆盖：agent 活动聚合端点（五类事件源：运行/尝试/任务事件/任务日志/
审计事件的装配、级别判定与 risk_score 回退、全量过滤参数、分页与
scan_limit 钳制、任务/项目上下文回填）、工作区活动端点（跨 Agent
聚合、agent_id 过滤、审计行无 Agent 关联时跳过、actor_agent_id 回推）、
活动事件游标分页（游标编解码、非法游标 400、全过滤参数、实体富化）、
agent 任务/项目/交互三维统计（触达集合、提交率、活跃度分数、HAVING
区间过滤、排序回退）、shared 辅助与审计降级查询。
"""

import itertools
from datetime import datetime

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy.exc import OperationalError

from models import (
    Agent,
    AgentActivityEvent,
    AgentAuditEvent,
    AgentRun,
    AgentTaskAttempt,
    AgentTaskAttemptState,
    AgentTaskEvent,
    AgentStatus,
    Organization,
    Project,
    Task,
    TaskLog,
    TaskLogActorType,
    User,
    db,
)

_TASK_ID_SEQ = itertools.count(50_000_000)


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    ctx.pop()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


def _mk_user(prefix="iu"):
    u = User(username=f"{prefix}_{uuid4hex()}", email=f"{prefix}_{uuid4hex()}@t.io")
    db.session.add(u)
    db.session.flush()
    return u


def uuid4hex():
    import uuid
    return uuid.uuid4().hex[:8]


def _mk_org(owner):
    org = Organization(name=f"o_{uuid4hex()}", slug=f"o_{uuid4hex()}",
                       owner_id=owner.id)
    db.session.add(org)
    db.session.flush()
    return org


def _mk_agent(workspace_id, creator, **kw):
    row = Agent(workspace_id=workspace_id, owner_id=creator.id,
                creator_user_id=creator.id, name=f"ag_{uuid4hex()}",
                status=AgentStatus.ACTIVE, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_project(owner, org=None):
    row = Project(name=f"p_{uuid4hex()}", owner_id=owner.id,
                  organization_id=org.id if org else None)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_task(owner, project, **kw):
    defaults = {"id": next(_TASK_ID_SEQ), "title": f"t_{uuid4hex()}",
                "content": "content", "status": "TODO",
                "priority": "MEDIUM", "project_id": project.id,
                "owner_id": owner.id}
    defaults.update(kw)
    row = Task(**defaults)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_run(env, run_id=None, state="queued", scheduled=None, payload=None, **kw):
    row = AgentRun(run_id=run_id or f"run_{uuid4hex()}",
                   workspace_id=env["org"].id, agent_id=env["agent"].id,
                   state=state, scheduled_at=scheduled or datetime(2026, 9, 1, 12, 0),
                   input_payload=payload, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_attempt(env, task, state=AgentTaskAttemptState.CREATED,
                started=None, ended=None, **kw):
    row = AgentTaskAttempt(
        attempt_id=f"att_{uuid4hex()}", task_id=task.id,
        agent_id=env["agent"].id, workspace_id=env["org"].id,
        state=state, lease_id=f"lease_{uuid4hex()}",
        started_at=started or datetime(2026, 9, 2, 9, 0),
        ended_at=ended, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_task_event(env, task, event_type="status_changed",
                   at=None, message=None, **kw):
    row = AgentTaskEvent(task_id=task.id, attempt_id="att_x",
                         agent_id=env["agent"].id, workspace_id=env["org"].id,
                         event_type=event_type, seq=1,
                         event_timestamp=at or datetime(2026, 9, 3, 10, 0),
                         message=message, **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_agent_log(env, task, content="agent did work",
                  at=None, **kw):
    row = TaskLog(task_id=task.id, actor_type=TaskLogActorType.AGENT,
                  actor_agent_id=env["agent"].id, content=content,
                  content_type="text/markdown",
                  created_at=at or datetime(2026, 9, 4, 8, 0), **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_user_log(user, task, content="human note", at=None):
    row = TaskLog(task_id=task.id, actor_type=TaskLogActorType.HUMAN,
                  actor_user_id=user.id, content=content,
                  content_type="text/markdown",
                  created_at=at or datetime(2026, 9, 5, 8, 0))
    db.session.add(row)
    db.session.flush()
    return row


def _mk_audit(env, event_type="tool.call", at=None, **kw):
    defaults = {
        "workspace_id": env["org"].id, "event_type": event_type,
        "actor_type": "agent", "actor_id": str(env["agent"].id),
        "target_type": "task", "target_id": "1",
        "occurred_at": at or datetime(2026, 9, 6, 7, 0),
    }
    defaults.update(kw)
    row = AgentAuditEvent(**defaults)
    db.session.add(row)
    db.session.flush()
    return row


def _mk_aevent(env, at=None, **kw):
    defaults = {
        "workspace_id": env["org"].id, "source": "agent_audit",
        "event_type": "task.leased", "level": "info",
        "occurred_at": at or datetime(2026, 9, 7, 6, 0),
    }
    defaults.update(kw)
    row = AgentActivityEvent(**defaults)
    db.session.add(row)
    db.session.flush()
    return row


def _headers_for(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


@pytest.fixture
def env(_isolated_app):
    user = _mk_user("ow")
    org = _mk_org(user)
    agent = _mk_agent(org.id, user)
    project = _mk_project(user, org=org)
    task = _mk_task(user, project)
    db.session.commit()
    return {
        "user": user, "org": org, "agent": agent,
        "project": project, "task": task,
        "headers": _headers_for(user),
        "base": "/todo-for-ai/api/v1",
    }


def _insights_url(env, path):
    return (f"{env['base']}/workspaces/{env['org'].id}"
            f"/agents/{env['agent'].id}/insights/{path}")


# ─────────────────────────── shared 辅助 ───────────────────────────


class TestSharedHelpers:
    def test_iso_and_parse_roundtrip(self):
        from api.agent_workspace_insights.shared import _iso, _parse_iso_datetime
        dt = datetime(2026, 9, 1, 8, 30)
        assert _iso(dt) == "2026-09-01T08:30:00"
        assert _iso(None) is None
        assert _parse_iso_datetime("2026-09-01T08:30:00") == dt
        assert _parse_iso_datetime("2026-09-01T08:30:00Z") == dt
        assert _parse_iso_datetime("garbage") is None
        assert _parse_iso_datetime("") is None
        assert _parse_iso_datetime(None) is None

    def test_parse_source_filter(self):
        from api.agent_workspace_insights.shared import _parse_source_filter
        assert _parse_source_filter("") == set()
        assert _parse_source_filter(None) == set()
        assert _parse_source_filter("Agent_Run, AUDIT") == {"agent_run", "audit"}

    def test_value_to_int_list(self):
        from api.agent_workspace_insights.shared import _value_to_int_list
        assert _value_to_int_list(None) == []
        assert _value_to_int_list([]) == []
        assert _value_to_int_list([1, "2", "x"]) == [1, 2]
        assert _value_to_int_list("7") == [7]

    def test_parse_int_optional(self):
        from api.agent_workspace_insights.shared import _parse_int_optional
        assert _parse_int_optional(None) is None
        assert _parse_int_optional(" 12 ") == 12
        assert _parse_int_optional("x") is None

    def test_safe_text_truncates(self):
        from api.agent_workspace_insights.shared import _safe_text
        assert _safe_text(None) == ""
        assert _safe_text("short") == "short"
        assert len(_safe_text("a" * 300)) == 243
        assert _safe_text("a" * 300).endswith("...")

    def test_activity_sort_key_handles_variants(self):
        from api.agent_workspace_insights.shared import _activity_sort_key
        dt = datetime(2026, 9, 1)
        assert _activity_sort_key({"occurred_at": dt, "_sort_id": 2}) == (dt, 2)
        assert _activity_sort_key({"occurred_at": "2026-09-01T00:00:00"}) == \
            (dt, 0)
        assert _activity_sort_key({})[0] == datetime.min

    def test_serialize_activity_item(self):
        from api.agent_workspace_insights.shared import _serialize_activity_item
        item = {"occurred_at": datetime(2026, 9, 1), "_sort_id": 9, "x": 1}
        out = _serialize_activity_item(item)
        assert out["occurred_at"] == "2026-09-01T00:00:00"
        assert "_sort_id" not in out
        assert out["x"] == 1

    def test_fetch_agent_audit_rows_schema_mismatch_degrades(self, env):
        from api.agent_workspace_insights.shared import _fetch_agent_audit_rows

        class _Boom:
            def order_by(self, *a, **kw):
                raise OperationalError(
                    "stmt", {}, "Unknown column 'x' in 'agent_audit_events'")

        assert _fetch_agent_audit_rows(_Boom(), 10, "ep") == []

    def test_fetch_agent_audit_rows_other_errors_raise(self, env):
        from api.agent_workspace_insights.shared import _fetch_agent_audit_rows

        class _Boom:
            def order_by(self, *a, **kw):
                raise OperationalError("stmt", {}, "database is locked")

        with pytest.raises(OperationalError):
            _fetch_agent_audit_rows(_Boom(), 10, "ep")

    def test_fetch_agent_audit_rows_normal_path(self, env):
        from api.agent_workspace_insights.shared import _fetch_agent_audit_rows
        row = _mk_audit(env)
        db.session.commit()
        query = AgentAuditEvent.query.filter(
            AgentAuditEvent.workspace_id == env["org"].id)
        rows = _fetch_agent_audit_rows(query, 10, "ep")
        assert [r.id for r in rows] == [row.id]

    def test_context_and_name_maps(self, env):
        from api.agent_workspace_insights.shared import (
            _build_agent_profile_map,
            _build_project_name_map,
            _build_task_context_map,
        )
        assert _build_task_context_map(set()) == {}
        assert _build_project_name_map(set()) == {}
        assert _build_agent_profile_map(set()) == {}

        tasks = _build_task_context_map({env["task"].id})
        assert tasks[env["task"].id]["task_title"] == env["task"].title
        assert tasks[env["task"].id]["project_name"] == env["project"].name
        assert _build_project_name_map({env["project"].id}) == \
            {env["project"].id: env["project"].name}
        profiles = _build_agent_profile_map({env["agent"].id})
        assert profiles[env["agent"].id]["name"] == env["agent"].name

    def test_touched_task_ids_subquery_union(self, env):
        from api.agent_workspace_insights.shared import _touched_task_ids_subquery
        other_task = _mk_task(env["user"], env["project"])
        _mk_attempt(env, env["task"])
        _mk_agent_log(env, other_task)
        db.session.commit()
        subq = _touched_task_ids_subquery(env["org"].id, env["agent"].id)
        ids = {int(r.task_id) for r in db.session.query(subq.c.task_id).all()}
        assert ids == {env["task"].id, other_task.id}


# ─────────────────────────── agent 活动聚合 ───────────────────────────


class TestAgentActivityEndpoint:
    def test_agent_missing_404(self, env, client):
        resp = client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/insights/activity", headers=env["headers"])
        assert resp.status_code == 404

    def test_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_empty_feed(self, env, client):
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["items"] == []
        assert data["pagination"]["total"] == 0
        assert data["summary"]["scan_limit"] >= 400

    def test_run_row_level_and_payload_extraction(self, env, client):
        _mk_run(env, state="failed", payload={"task_id": str(env["task"].id),
                                              "project_id": env["project"].id},
                failure_reason="boom", failure_code="E_X")
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 1
        item = items[0]
        assert item["source"] == "agent_run"
        assert item["event_type"] == "run.failed"
        assert item["level"] == "error"
        assert item["message"] == f"Run {item['run_id']} failed: boom"
        assert item["task_id"] == env["task"].id
        assert item["project_id"] == env["project"].id
        assert item["task_title"] == env["task"].title
        assert item["project_name"] == env["project"].name
        assert resp.get_json()["data"]["summary"]["levels"]["error"] == 1

    def test_run_with_project_only_payload_resolves_project_name(
            self, env, client):
        _mk_run(env, state="queued", payload={"project_id": env["project"].id})
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["task_id"] is None
        assert item["project_name"] == env["project"].name

    def test_attempt_row_aborted_is_error(self, env, client):
        _mk_attempt(env, env["task"], state=AgentTaskAttemptState.ABORTED,
                    ended=datetime(2026, 9, 2, 10, 0),
                    failure_reason="abort reason")
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["source"] == "agent_task_attempt"
        assert item["event_type"] == "attempt.aborted"
        assert item["level"] == "error"
        assert "abort reason" in item["message"]
        assert item["occurred_at"].startswith("2026-09-02T10")

    def test_task_event_error_level_and_default_message(self, env, client):
        _mk_task_event(env, env["task"], event_type="Error")
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["event_type"] == "event.error"
        assert item["level"] == "error"
        assert item["message"] == "Event error"

    def test_log_row_actor_fields(self, env, client):
        _mk_agent_log(env, env["task"], content="log " + "x" * 400)
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["source"] == "task_log"
        assert item["actor_type"] == "agent"
        assert item["actor_agent_id"] == env["agent"].id
        assert len(item["message"]) == 303  # 300 + "..."

    def test_audit_level_fallback_by_risk_score(self, env, client):
        _mk_audit(env, level="critical", risk_score=60,
                  at=datetime(2026, 9, 6, 7, 0))
        _mk_audit(env, level="critical", risk_score=25,
                  at=datetime(2026, 9, 6, 8, 0))
        _mk_audit(env, level="critical", risk_score=5,
                  at=datetime(2026, 9, 6, 9, 0))
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        levels = [i["level"] for i in resp.get_json()["data"]["items"]]
        assert levels == ["info", "warn", "error"]

    def test_matcher_branches_unit(self):
        from api.agent_workspace_insights.shared import _activity_item_matches
        item = {
            "source": "agent_run", "level": "error",
            "event_type": "run.failed", "message": "boom",
            "payload": {"k": "v"}, "task_id": 7, "project_id": 3,
            "run_id": "run-abc", "attempt_id": "att-xyz",
            "actor_type": "agent", "risk_score": 40,
        }

        def m(**kw):
            return _activity_item_matches(item=item, **kw)

        assert m(source_filter={"agent_run"}, level_filter=set(),
                 event_type_filter="", query_text="")
        assert not m(source_filter={"task_log"}, level_filter=set(),
                     event_type_filter="", query_text="")
        assert not m(source_filter=set(), level_filter={"warn"},
                     event_type_filter="", query_text="")
        assert m(source_filter=set(), level_filter={"error"},
                 event_type_filter="", query_text="")
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="fail", query_text="")
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="lease", query_text="")
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", task_id_filter=7)
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="", task_id_filter=8)
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", project_id_filter=3)
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="", project_id_filter=4)
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", run_id_filter="abc")
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="", run_id_filter="zz")
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", attempt_id_filter="xyz")
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="",
                     attempt_id_filter="zz")
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", actor_type_filter="agent")
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="",
                     actor_type_filter="user")
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", min_risk_score=40)
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="", min_risk_score=41)
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="", max_risk_score=40)
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="", max_risk_score=39)
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text="boom")
        assert m(source_filter=set(), level_filter=set(),
                 event_type_filter="", query_text='"k": "v"')
        assert not m(source_filter=set(), level_filter=set(),
                     event_type_filter="", query_text="absent-text")
        # risk_score 缺失时 min/max 均判不匹配
        naked = dict(item, risk_score=None)
        assert not _activity_item_matches(
            item=naked, source_filter=set(), level_filter=set(),
            event_type_filter="", query_text="", min_risk_score=1)
        assert not _activity_item_matches(
            item=naked, source_filter=set(), level_filter=set(),
            event_type_filter="", query_text="", max_risk_score=100)

    def test_audit_task_and_project_relation(self, env, client):
        _mk_audit(env, task_id=env["task"].id, project_id=env["project"].id)
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["task_id"] == env["task"].id
        assert item["project_id"] == env["project"].id
        assert item["project_name"] == env["project"].name

    def test_filters_matrix(self, env, client):
        t2 = _mk_task(env["user"], env["project"])
        _mk_run(env, state="queued", scheduled=datetime(2026, 9, 1, 0, 0))
        _mk_attempt(env, t2, started=datetime(2026, 9, 2, 0, 0))
        _mk_task_event(env, t2, event_type="commented",
                       at=datetime(2026, 9, 3, 0, 0))
        _mk_agent_log(env, env["task"], content="special-keyword",
                      at=datetime(2026, 9, 4, 0, 0))
        _mk_audit(env, event_type="secret.read", level="warn",
                  risk_score=30, run_id="run-abc-777",
                  at=datetime(2026, 9, 6, 0, 0))
        db.session.commit()
        url = _insights_url(env, "activity")

        def ids(query):
            resp = client.get(f"{url}{query}", headers=env["headers"])
            return [i["source"] for i in resp.get_json()["data"]["items"]]

        assert ids("?source=agent_run") == ["agent_run"]
        assert ids("?source=agent_run,agent_task_attempt") == \
            ["agent_task_attempt", "agent_run"]
        assert ids("?level=error") == []  # 本例全部为 info/warn
        assert ids("?event_type=audit") == ["agent_audit"]
        assert ids(f"?task_id={env['task'].id}") == ["task_log"]
        assert ids(f"?project_id={env['project'].id}") == [
            "task_log", "agent_task_event", "agent_task_attempt"]
        assert ids("?run_id=abc-777") == ["agent_audit"]
        assert ids("?q=special-keyword") == ["task_log"]
        assert ids("?actor_type=agent") == [
            "agent_audit", "task_log"]
        assert ids("?min_risk_score=20") == ["agent_audit"]
        # max_risk_score 只保留"有分且 ≤ 阈值"的项；无分项一律滤除
        assert ids("?max_risk_score=10") == []

    def test_time_window_filters(self, env, client):
        _mk_run(env, scheduled=datetime(2026, 9, 1, 0, 0))
        _mk_run(env, state="succeeded", scheduled=datetime(2026, 9, 15, 0, 0))
        db.session.commit()
        url = _insights_url(env, "activity")
        resp = client.get(f"{url}?from=2026-09-10T00:00:00",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["state"] for i in items] == ["succeeded"]
        resp = client.get(f"{url}?to=2026-09-10T00:00:00",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["state"] for i in items] == ["queued"]

    def test_pagination_and_scan_limit_clamp(self, env, client):
        for hour in (1, 2, 3):
            _mk_run(env, state="queued",
                    scheduled=datetime(2026, 9, 1, hour, 0))
        db.session.commit()
        url = _insights_url(env, "activity")

        resp = client.get(f"{url}?page=2&per_page=2", headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["pagination"]["total"] == 3
        assert data["pagination"]["has_prev"] is True
        assert data["pagination"]["has_next"] is False
        assert len(data["items"]) == 1

        resp = client.get(f"{url}?per_page=500", headers=env["headers"])
        assert resp.get_json()["data"]["pagination"]["per_page"] == 100

        resp = client.get(f"{url}?scan_limit=99999", headers=env["headers"])
        assert resp.get_json()["data"]["summary"]["scan_limit"] == 4000
        resp = client.get(f"{url}?scan_limit=1", headers=env["headers"])
        assert resp.get_json()["data"]["summary"]["scan_limit"] == 100

    def test_sorted_by_occurred_at_desc(self, env, client):
        _mk_task_event(env, env["task"], at=datetime(2026, 9, 3, 0, 0))
        _mk_run(env, scheduled=datetime(2026, 9, 10, 0, 0))
        _mk_agent_log(env, env["task"], at=datetime(2026, 9, 4, 0, 0))
        db.session.commit()
        resp = client.get(_insights_url(env, "activity"),
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["source"] for i in items] == \
            ["agent_run", "task_log", "agent_task_event"]


# ─────────────────────────── 工作区活动聚合 ───────────────────────────


class TestWorkspaceActivitiesEndpoint:
    def _url(self, env):
        return f"{env['base']}/workspaces/{env['org'].id}/insights/activities"

    def test_workspace_missing_404(self, env, client):
        resp = client.get(f"{env['base']}/workspaces/999999/insights/activities",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(self._url(env), headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_cross_agent_aggregation_with_names(self, env, client):
        agent2 = _mk_agent(env["org"].id, env["user"])
        _mk_run(env, state="failed", scheduled=datetime(2026, 9, 1, 0, 0))
        row2 = AgentRun(run_id="run-a2", workspace_id=env["org"].id,
                        agent_id=agent2.id, state="queued",
                        scheduled_at=datetime(2026, 9, 2, 0, 0))
        db.session.add(row2)
        db.session.commit()
        resp = client.get(self._url(env), headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["source"] for i in items] == ["agent_run", "agent_run"]
        # 按 occurred_at desc：agent2 的 run（9-2）在前
        assert items[0]["agent_id"] == agent2.id
        assert items[0]["agent_name"] == agent2.name
        assert items[1]["agent_id"] == env["agent"].id

    def test_agent_id_filter(self, env, client):
        agent2 = _mk_agent(env["org"].id, env["user"])
        _mk_run(env, scheduled=datetime(2026, 9, 1, 0, 0))
        db.session.add(AgentRun(run_id="run-a2",
                                workspace_id=env["org"].id,
                                agent_id=agent2.id, state="queued",
                                scheduled_at=datetime(2026, 9, 2, 0, 0)))
        db.session.commit()
        resp = client.get(f"{self._url(env)}?agent_id={agent2.id}",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["agent_id"] for i in items] == [agent2.id]

    def test_audit_row_without_agent_relation_skipped(self, env, client):
        # actor/target 均非 agent：不进结果集
        _mk_audit(env, actor_type="user", actor_id="9",
                  target_type="task", target_id="5")
        # actor 标为 agent 但 id 不可解析：命中查询但被跳过
        _mk_audit(env, actor_type="agent", actor_id="not-a-number",
                  target_type="task", target_id="5")
        db.session.commit()
        resp = client.get(self._url(env), headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

    def test_audit_with_project_only_gets_project_name(self, env, client):
        _mk_audit(env, project_id=env["project"].id, task_id=None)
        db.session.commit()
        resp = client.get(self._url(env), headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        assert item["task_id"] is None
        assert item["project_name"] == env["project"].name

    def test_audit_actor_agent_id_fallback_from_actor_id(self, env, client):
        row = _mk_audit(env, actor_agent_id=None, target_agent_id=None)
        db.session.commit()
        resp = client.get(self._url(env), headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["agent_id"] == env["agent"].id
        assert items[0]["actor_agent_id"] == env["agent"].id

    def test_time_window(self, env, client):
        _mk_run(env, scheduled=datetime(2026, 9, 1, 0, 0))
        _mk_run(env, state="succeeded", scheduled=datetime(2026, 9, 20, 0, 0))
        db.session.commit()
        resp = client.get(f"{self._url(env)}?from=2026-09-15T00:00:00",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["state"] for i in items] == ["succeeded"]
        resp = client.get(f"{self._url(env)}?to=2026-09-15T00:00:00",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["state"] for i in items] == ["queued"]

    def test_all_sources_aggregated(self, env, client):
        task2 = _mk_task(env["user"], env["project"])
        _mk_run(env, state="failed", scheduled=datetime(2026, 9, 1, 0, 0),
                payload={"task_id": env["task"].id,
                         "project_id": env["project"].id},
                failure_reason="runner died")
        _mk_attempt(env, task2, state=AgentTaskAttemptState.ABORTED,
                    started=datetime(2026, 9, 2, 0, 0),
                    ended=datetime(2026, 9, 2, 1, 0),
                    failure_reason="no lease")
        _mk_task_event(env, task2, event_type="failed",
                       at=datetime(2026, 9, 3, 0, 0))
        _mk_agent_log(env, env["task"], at=datetime(2026, 9, 4, 0, 0))
        _mk_audit(env, task_id=env["task"].id, project_id=env["project"].id,
                  level="critical", risk_score=70,
                  at=datetime(2026, 9, 6, 0, 0))
        db.session.commit()
        resp = client.get(self._url(env), headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["summary"]["sources"] == {
            "agent_run": 1, "agent_task_attempt": 1, "agent_task_event": 1,
            "task_log": 1, "agent_audit": 1}
        items = {i["source"]: i for i in data["items"]}

        run = items["agent_run"]
        assert run["level"] == "error"
        assert "runner died" in run["message"]
        assert run["task_title"] == env["task"].title
        assert run["project_name"] == env["project"].name
        assert run["agent_name"] == env["agent"].name

        attempt = items["agent_task_attempt"]
        assert attempt["level"] == "error"
        assert attempt["task_title"] == task2.title

        assert items["agent_task_event"]["event_type"] == "event.failed"
        assert items["task_log"]["actor_type"] == "agent"

        audit = items["agent_audit"]
        assert audit["level"] == "error"
        assert audit["task_id"] == env["task"].id
        assert audit["project_name"] == env["project"].name
        assert audit["agent_id"] == env["agent"].id

    def test_audit_target_agent_fallback(self, env, client):
        # actor 是人类、target 是 agent：经 target 回推 agent 归属
        _mk_audit(env, actor_type="user", actor_id="9",
                  target_type="agent",
                  target_id=str(env["agent"].id))
        db.session.commit()
        resp = client.get(self._url(env), headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["agent_id"] == env["agent"].id
        assert items[0]["target_agent_id"] == env["agent"].id
        assert items[0]["actor_agent_id"] is None

    def test_scan_limit_clamp(self, env, client):
        resp = client.get(f"{self._url(env)}?scan_limit=999999",
                          headers=env["headers"])
        assert resp.get_json()["data"]["summary"]["scan_limit"] == 6000


# ─────────────────────────── 活动事件（游标分页）───────────────────────────


class TestActivityEventsEndpoint:
    def _url(self, env):
        return (f"{env['base']}/workspaces/{env['org'].id}"
                f"/insights/activity-events")

    def test_workspace_missing_404(self, env, client):
        resp = client.get(
            f"{env['base']}/workspaces/999999/insights/activity-events",
            headers=env["headers"])
        assert resp.status_code == 404

    def test_stranger_forbidden(self, env, client):
        stranger = _mk_user("st")
        db.session.commit()
        resp = client.get(self._url(env), headers=_headers_for(stranger))
        assert resp.status_code == 403

    def test_empty_and_limit_clamp(self, env, client):
        resp = client.get(self._url(env), headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["items"] == []
        assert data["page_info"]["has_more"] is False
        assert data["page_info"]["next_cursor"] is None
        assert data["page_info"]["limit"] == 50

        assert client.get(f"{self._url(env)}?limit=0",
                          headers=env["headers"]
                          ).get_json()["data"]["page_info"]["limit"] == 1
        assert client.get(f"{self._url(env)}?limit=99999",
                          headers=env["headers"]
                          ).get_json()["data"]["page_info"]["limit"] == 200
        assert client.get(f"{self._url(env)}?limit=abc",
                          headers=env["headers"]
                          ).get_json()["data"]["page_info"]["limit"] == 50

    def test_invalid_cursor_400(self, env, client):
        assert client.get(f"{self._url(env)}?cursor=not-a-cursor",
                          headers=env["headers"]).status_code == 400
        assert client.get(f"{self._url(env)}?cursor=2026-09-01T00:00:00|abc",
                          headers=env["headers"]).status_code == 400

    def test_cursor_pagination_roundtrip(self, env, client):
        for hour in (1, 2, 3):
            _mk_aevent(env, at=datetime(2026, 9, 7, hour, 0),
                       message=f"m{hour}")
        db.session.commit()
        url = self._url(env)

        first = client.get(f"{url}?limit=2", headers=env["headers"])
        page1 = first.get_json()["data"]
        assert [i["message"] for i in page1["items"]] == ["m3", "m2"]
        assert page1["page_info"]["has_more"] is True
        cursor = page1["page_info"]["next_cursor"]
        assert cursor and "|" in cursor

        second = client.get(f"{url}?limit=2&cursor={cursor}",
                            headers=env["headers"])
        page2 = second.get_json()["data"]
        assert [i["message"] for i in page2["items"]] == ["m1"]
        assert page2["page_info"]["has_more"] is False

    def test_filters_and_entity_enrichment(self, env, client):
        agent2 = _mk_agent(env["org"].id, env["user"])
        other_project = _mk_project(env["user"], org=env["org"])
        _mk_aevent(env, source="agent_audit", level="warn",
                   event_type="secret.read", agent_id=env["agent"].id,
                   task_id=env["task"].id, project_id=env["project"].id,
                   run_id="run-zz-9", attempt_id="att-zz-9",
                   message="touched secret")
        _mk_aevent(env, source="mcp", level="info",
                   event_type="task.leased", agent_id=agent2.id,
                   at=datetime(2026, 9, 8, 6, 0))
        db.session.commit()
        url = self._url(env)

        def count(query):
            resp = client.get(f"{url}{query}", headers=env["headers"])
            return len(resp.get_json()["data"]["items"])

        assert count("?source=agent_audit") == 1
        assert count("?level=warn") == 1
        assert count("?event_type=secret") == 1
        assert count(f"?agent_id={agent2.id}") == 1
        assert count(f"?task_id={env['task'].id}") == 1
        assert count(f"?project_id={env['project'].id}") == 1
        assert count("?run_id=zz-9") == 1
        assert count("?attempt_id=zz-9") == 1
        assert count("?q=secret") == 1
        assert count("?from=2026-09-08T00:00:00") == 1
        assert count("?to=2026-09-07T12:00:00") == 1

        resp = client.get(url, headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        enriched = [i for i in items if i["source"] == "agent_audit"][0]
        assert enriched["agent"]["id"] == env["agent"].id
        assert enriched["agent"]["name"] == env["agent"].name
        assert enriched["task"]["title"] == env["task"].title
        assert enriched["task"]["project_name"] == env["project"].name
        assert enriched["project"]["name"] == env["project"].name


# ─────────────────────────── agent 任务统计 ───────────────────────────


class TestAgentTasksEndpoint:
    def test_agent_missing_and_stranger(self, env, client):
        assert client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/insights/tasks", headers=env["headers"]).status_code == 404
        stranger = _mk_user("st")
        db.session.commit()
        assert client.get(_insights_url(env, "tasks"),
                          headers=_headers_for(stranger)).status_code == 403

    def test_touch_via_attempt_and_log(self, env, client):
        task2 = _mk_task(env["user"], env["project"])
        _mk_attempt(env, env["task"],
                    state=AgentTaskAttemptState.COMMITTED,
                    started=datetime(2026, 9, 2, 9, 0),
                    ended=datetime(2026, 9, 2, 10, 0))
        _mk_agent_log(env, task2, at=datetime(2026, 9, 4, 8, 0))
        db.session.commit()
        resp = client.get(_insights_url(env, "tasks"),
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["pagination"]["total"] == 2
        by_id = {i["task_id"]: i for i in data["items"]}
        assert by_id[env["task"].id]["last_attempt"]["state"] == "committed"
        assert by_id[env["task"].id]["agent_log_count"] == 0
        assert by_id[task2.id]["last_attempt"] is None
        assert by_id[task2.id]["agent_log_count"] == 1
        assert by_id[task2.id]["last_activity_at"].startswith("2026-09-04")

    def test_status_search_project_filters(self, env, client):
        task2 = _mk_task(env["user"], env["project"], status="DONE")
        project2 = _mk_project(env["user"], org=env["org"])
        task3 = _mk_task(env["user"], project2, title="findme-xyz")
        for t in (env["task"], task2, task3):
            _mk_attempt(env, t)
        db.session.commit()
        url = _insights_url(env, "tasks")

        assert client.get(f"{url}?status=warp",
                          headers=env["headers"]).status_code == 400
        resp = client.get(f"{url}?status=done", headers=env["headers"])
        assert resp.get_json()["data"]["pagination"]["total"] == 1
        resp = client.get(f"{url}?search=findme", headers=env["headers"])
        assert resp.get_json()["data"]["pagination"]["total"] == 1
        resp = client.get(f"{url}?project_id={project2.id}",
                          headers=env["headers"])
        assert resp.get_json()["data"]["pagination"]["total"] == 1


# ─────────────────────────── agent 项目统计 ───────────────────────────


class TestAgentProjectsEndpoint:
    def test_agent_missing_and_stranger(self, env, client):
        assert client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/insights/projects", headers=env["headers"]).status_code == 404
        stranger = _mk_user("st")
        db.session.commit()
        assert client.get(_insights_url(env, "projects"),
                          headers=_headers_for(stranger)).status_code == 403

    def test_empty_set_returns_empty_page(self, env, client):
        resp = client.get(_insights_url(env, "projects"),
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["items"] == []
        assert data["pagination"]["total"] == 0

    def test_stats_scores_and_flags(self, env, client):
        env["agent"].allowed_project_ids = [env["project"].id]
        for _ in range(3):
            _mk_attempt(env, _mk_task(env["user"], env["project"]),
                        state=AgentTaskAttemptState.COMMITTED,
                        started=datetime(2026, 9, 2, 9, 0),
                        ended=datetime(2026, 9, 2, 10, 0))
        _mk_attempt(env, _mk_task(env["user"], env["project"]),
                    state=AgentTaskAttemptState.ABORTED,
                    started=datetime(2026, 9, 3, 9, 0))
        _mk_agent_log(env, env["task"], at=datetime(2026, 9, 5, 8, 0))
        db.session.commit()

        resp = client.get(_insights_url(env, "projects"),
                          headers=env["headers"])
        data = resp.get_json()["data"]
        assert data["pagination"]["total"] == 1
        item = data["items"][0]
        assert item["project_id"] == env["project"].id
        assert item["is_explicitly_allowed"] is True
        assert item["touched_task_count"] == 4
        assert item["committed_task_count"] == 3
        assert item["interaction_log_count"] == 1
        assert item["submission_rate"] == 75.0
        assert 0 <= item["activity_score"] <= 100
        assert item["last_activity_at"].startswith("2026-09-05")

    def test_search_and_sort_fallback(self, env, client):
        p2 = _mk_project(env["user"], org=env["org"])
        p2.name = "zzz-special"
        db.session.add(p2)
        env["agent"].allowed_project_ids = [env["project"].id, p2.id]
        db.session.commit()
        url = _insights_url(env, "projects")

        resp = client.get(f"{url}?search=zzz", headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["project_name"] for i in items] == ["zzz-special"]

        resp = client.get(f"{url}?sort_by=nonexistent&sort_order=asc",
                          headers=env["headers"])
        assert resp.status_code == 200

        resp = client.get(f"{url}?sort_by=project_name&sort_order=asc",
                          headers=env["headers"])
        names = [i["project_name"] for i in resp.get_json()["data"]["items"]]
        assert names == sorted(names)

        resp = client.get(f"{url}?sort_by=project_id", headers=env["headers"])
        ids = [i["project_id"] for i in resp.get_json()["data"]["items"]]
        assert ids == sorted(ids, reverse=True)

    def test_score_floor_for_stale_project(self, env, client):
        env["agent"].allowed_project_ids = [env["project"].id]
        _mk_attempt(env, env["task"],
                    state=AgentTaskAttemptState.COMMITTED,
                    started=datetime(2020, 1, 1, 0, 0),
                    ended=datetime(2020, 1, 1, 1, 0))
        db.session.commit()
        resp = client.get(_insights_url(env, "projects"),
                          headers=env["headers"])
        item = resp.get_json()["data"]["items"][0]
        # 提交率 100×0.4 + 交互 1 天×10 + 久远活跃衰减 0 = 50
        assert item["activity_score"] == 50.0


# ─────────────────────────── agent 交互统计 ───────────────────────────


class TestAgentInteractionsEndpoint:
    def _setup_two_users(self, env):
        alice = _mk_user("alice")
        bob = _mk_user("bob")
        alice.full_name = "Alice Wonder"
        _mk_attempt(env, env["task"])  # 触达集合
        _mk_user_log(alice, env["task"], "a1",
                     at=datetime(2026, 9, 1, 8, 0))
        _mk_user_log(alice, env["task"], "a2",
                     at=datetime(2026, 9, 2, 8, 0))
        _mk_user_log(bob, env["task"], "b1",
                     at=datetime(2026, 9, 3, 8, 0))
        db.session.commit()
        return alice, bob

    def test_agent_missing_and_stranger(self, env, client):
        assert client.get(
            f"{env['base']}/workspaces/{env['org'].id}/agents/999999"
            f"/insights/interactions", headers=env["headers"]).status_code == 404
        stranger = _mk_user("st")
        db.session.commit()
        assert client.get(_insights_url(env, "interactions"),
                          headers=_headers_for(stranger)).status_code == 403

    def test_empty(self, env, client):
        resp = client.get(_insights_url(env, "interactions"),
                          headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

    def test_aggregation_and_display_name(self, env, client):
        alice, bob = self._setup_two_users(env)
        resp = client.get(_insights_url(env, "interactions"),
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert len(items) == 2
        top = items[0]  # 默认按 last_interaction_at desc → bob 先? alice 最后交互 9-2, bob 9-3
        by_user = {i["user_id"]: i for i in items}
        assert by_user[alice.id]["interaction_count"] == 2
        assert by_user[alice.id]["display_name"] == "Alice Wonder"
        assert by_user[alice.id]["task_count"] == 1
        assert by_user[alice.id]["project_count"] == 1
        assert by_user[alice.id]["avg_content_length"] == 2
        assert by_user[bob.id]["display_name"] == bob.username

    def test_search_time_and_having_filters(self, env, client):
        alice, bob = self._setup_two_users(env)
        url = _insights_url(env, "interactions")

        resp = client.get(f"{url}?search={bob.username[:8]}",
                          headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["user_id"] for i in items] == [bob.id]

        resp = client.get(f"{url}?min_interactions=2", headers=env["headers"])
        items = resp.get_json()["data"]["items"]
        assert [i["user_id"] for i in items] == [alice.id]

        resp = client.get(f"{url}?max_interactions=1", headers=env["headers"])
        assert [i["user_id"] for i in
                resp.get_json()["data"]["items"]] == [bob.id]

        resp = client.get(f"{url}?min_tasks=2", headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

        resp = client.get(f"{url}?max_tasks=0", headers=env["headers"])
        assert resp.get_json()["data"]["items"] == []

        resp = client.get(f"{url}?from=2026-09-03T00:00:00",
                          headers=env["headers"])
        assert [i["user_id"] for i in
                resp.get_json()["data"]["items"]] == [bob.id]

        resp = client.get(f"{url}?to=2026-09-02T23:59:59",
                          headers=env["headers"])
        assert [i["user_id"] for i in
                resp.get_json()["data"]["items"]] == [alice.id]

    def test_sort_fields(self, env, client):
        alice, bob = self._setup_two_users(env)
        url = _insights_url(env, "interactions")
        resp = client.get(f"{url}?sort_by=interaction_count&sort_order=asc",
                          headers=env["headers"])
        counts = [i["interaction_count"]
                  for i in resp.get_json()["data"]["items"]]
        assert counts == sorted(counts)
        resp = client.get(f"{url}?sort_by=user_id", headers=env["headers"])
        ids = [i["user_id"] for i in resp.get_json()["data"]["items"]]
        assert ids == sorted(ids, reverse=True)
        resp = client.get(f"{url}?sort_by=weird", headers=env["headers"])
        assert resp.status_code == 200
