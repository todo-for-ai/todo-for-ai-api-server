"""Agent 批量操作（services/agent_batch_ops.py）单元回归。

覆盖：CSV/JSON 导出（字段映射、过滤、时间序列化）、JSON 导入（create/update
模式、重名跳过、坏数据容错）、批量轮换/改状态/删除（命中、未命中、关联
Secret 阻拦、强制删除、异常统计）。结构为单一内聚类，故不拆文件，纯补测。
"""

import csv
import io
import json
from unittest.mock import MagicMock

import pytest

from models import Agent, AgentStatus, AgentSecret, User, db
from services.agent_batch_ops import AgentBatchOperations, get_batch_operations


@pytest.fixture(scope="function", autouse=True)
def _isolated_app(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
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
def user():
    import uuid
    u = User(username=f"bu_{uuid.uuid4().hex[:8]}", email=f"bu_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def agent(user):
    def _make(name="agent-a", workspace_id=1, **kw):
        row = Agent(
            workspace_id=workspace_id, owner_id=user.id,
            creator_user_id=user.id, name=name, status=AgentStatus.ACTIVE,
            **kw)
        db.session.add(row)
        db.session.commit()
        return row
    return _make


@pytest.fixture
def secret(user, agent):
    def _make(ag):
        row = AgentSecret(
            agent_id=ag.id, workspace_id=ag.workspace_id, name="api_key",
            secret_hash="h" * 64, secret_encrypted="cipher", prefix="sk-",
            created_by_user_id=user.id, updated_by_user_id=user.id,
        )
        db.session.add(row)
        db.session.commit()
        return row
    return _make


class TestExportCSV:
    def test_exports_all_workspace_agents(self, agent):
        agent(name="a1", capability_tags=["search", "code"], temperature=0.7)
        agent(name="a2", workspace_id=2)  # 其他工作区不可见

        csv_text = AgentBatchOperations().export_agents_to_csv(1)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "a1"
        assert json.loads(row["capability_tags"]) == ["search", "code"]
        assert row["created_at"] and "T" in row["created_at"]

    def test_filters_by_ids(self, agent):
        a1 = agent(name="keep")
        agent(name="drop")
        csv_text = AgentBatchOperations().export_agents_to_csv(1, [a1.id])
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        assert [r["name"] for r in rows] == ["keep"]


class TestExportJSON:
    def test_structure_and_fields(self, agent):
        agent(name="j1", display_name="J 一号", llm_provider="openai",
              llm_model="gpt-4", temperature=0.7, capability_tags=["sql"])
        data = AgentBatchOperations().export_agents_to_json(1)
        assert data["export_version"] == "1.0"
        assert data["workspace_id"] == 1
        assert data["agent_count"] == 1
        body = data["agents"][0]
        assert body["name"] == "j1"
        assert body["display_name"] == "J 一号"
        assert body["status"] == AgentStatus.ACTIVE.value
        assert body["capability_tags"] == ["sql"]
        assert body["temperature"].startswith("0.7")

    def test_filters_by_ids_for_json(self, agent):
        keep = agent(name="k1")
        agent(name="k2")
        data = AgentBatchOperations().export_agents_to_json(1, [keep.id])
        assert [a["name"] for a in data["agents"]] == ["k1"]

    def test_include_secrets_is_currently_noop(self, agent):
        """行为钉子：include_secrets 参数当前不改变导出内容。"""
        agent(name="s1")
        without = AgentBatchOperations().export_agents_to_json(1)
        with_flag = AgentBatchOperations().export_agents_to_json(1, include_secrets=True)
        without.pop("exported_at")
        with_flag.pop("exported_at")
        assert without == with_flag


class TestImportJSON:
    def test_create_mode_skips_existing(self, agent, user):
        agent(name="dup")
        ops = AgentBatchOperations()
        result = ops.import_agents_from_json(1, user.id, {"agents": [
            {"name": "dup"},
            {"name": "fresh", "display_name": "新", "capability_tags": ["x"]},
        ]})
        assert result["total"] == 2
        assert result["created"] == 1
        assert result["skipped"] == 1
        fresh = Agent.query.filter_by(name="fresh").one()
        assert fresh.display_name == "新"

    def test_update_mode_updates_existing(self, agent, user):
        existing = agent(name="dup", display_name="旧")
        result = AgentBatchOperations().import_agents_from_json(
            1, user.id, {"agents": [
                {"name": "dup", "display_name": "新名", "llm_model": "m2"},
                {"name": "brand-new"},
            ]}, import_mode="update")
        assert result["updated"] == 1 and result["created"] == 1
        assert existing.display_name == "新名"
        assert existing.llm_model == "m2"
        assert existing.updated_by_user_id == user.id

    def test_bad_entry_counted_as_failed(self, agent, user):
        result = AgentBatchOperations().import_agents_from_json(
            1, user.id, {"agents": [{"display_name": "no-name"}]})
        assert result["failed"] == 1
        assert result["errors"][0]["error"]  # KeyError 已转为消息


class TestRotateSecrets:
    def test_rotates_active_secrets(self, agent, secret, user, monkeypatch):
        ag = agent(name="rot")
        row = secret(ag)
        monkeypatch.setattr(row, "rotate_encryption", lambda uid: None)

        result = AgentBatchOperations().batch_rotate_secrets(1, [ag.id], user.id)
        assert result == {"total_agents": 1, "total_secrets": 1, "rotated": 1,
                          "failed": 0, "errors": []}

    def test_rotation_failure_counted(self, agent, secret, user):
        ag = agent(name="rot2")
        row = secret(ag)

        def boom(uid):
            raise RuntimeError("key gone")
        row.rotate_encryption = boom
        result = AgentBatchOperations().batch_rotate_secrets(1, [ag.id], user.id)
        assert result["failed"] == 1 and result["rotated"] == 0

    def test_query_failure_recorded(self, agent, user, monkeypatch):
        ag = agent(name="rot3")
        monkeypatch.setattr(
            AgentSecret, "query",
            MagicMock(filter_by=MagicMock(side_effect=RuntimeError("db"))))
        result = AgentBatchOperations().batch_rotate_secrets(1, [ag.id], user.id)
        assert result["errors"][0]["agent_id"] == ag.id


class TestBatchUpdateStatus:
    def test_updates_found_and_reports_missing(self, agent, user):
        a1 = agent(name="u1")
        result = AgentBatchOperations().batch_update_agent_status(
            1, [a1.id, 999999], AgentStatus.DISABLED, user.id)
        assert result["updated"] == 1 and result["failed"] == 1
        assert result["errors"] == [{"agent_id": 999999, "error": "Agent not found"}]
        assert a1.status == AgentStatus.DISABLED

    def test_exception_counted(self, agent, user, monkeypatch):
        a1 = agent(name="u2")
        monkeypatch.setattr(
            Agent, "query",
            MagicMock(filter_by=MagicMock(side_effect=RuntimeError("db"))))
        result = AgentBatchOperations().batch_update_agent_status(
            1, [a1.id], AgentStatus.ACTIVE, user.id)
        assert result["failed"] == 1
        assert "db" in result["errors"][0]["error"]


class TestBatchDelete:
    def test_delete_without_secrets(self, agent, user):
        a1 = agent(name="d1")
        result = AgentBatchOperations().batch_delete_agents(1, [a1.id], user.id)
        assert result["deleted"] == 1
        assert Agent.query.filter_by(id=a1.id).first() is None

    def test_blocked_by_secrets_unless_forced(self, agent, secret, user):
        a1 = agent(name="d2")
        secret(a1)
        blocked = AgentBatchOperations().batch_delete_agents(1, [a1.id], user.id)
        assert blocked["failed"] == 1
        assert "force=true" in blocked["errors"][0]["error"]

        forced = AgentBatchOperations().batch_delete_agents(
            1, [a1.id], user.id, force=True)
        assert forced["deleted"] == 1

    def test_missing_agent_reported(self, agent, user):
        result = AgentBatchOperations().batch_delete_agents(1, [424242], user.id)
        assert result["failed"] == 1
        assert result["errors"][0]["error"] == "Agent not found"

    def test_exception_counted(self, agent, user, monkeypatch):
        a1 = agent(name="d3")
        monkeypatch.setattr(
            Agent, "query",
            MagicMock(filter_by=MagicMock(side_effect=RuntimeError("db"))))
        result = AgentBatchOperations().batch_delete_agents(1, [a1.id], user.id)
        assert result["failed"] == 1


def test_get_batch_operations_singleton():
    first = get_batch_operations()
    assert get_batch_operations() is first
    assert isinstance(first, AgentBatchOperations)
