"""记忆用户开放 API 测试：授权矩阵 / 租户边界 / 编辑与遗忘 / 召回预览。

授权矩阵（写入按维度收紧，读取=组织成员）：
- organization → 组织 owner/admin；project → 项目 owner/maintainer；
- agent → Agent 所有者或组织管理者；user → 仅本人；session → 拒绝手工写入。
"""

import uuid

import pytest
from flask_jwt_extended import create_access_token

BASE_URL = "/todo-for-ai/api/v1/memory"


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
    from app import create_app
    from models import db

    app = create_app("testing")
    app.config.update({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {},
    })
    _ctx = app.app_context()
    _ctx.push()
    db.create_all()
    yield app
    db.session.remove()
    db.drop_all()
    _ctx.pop()


@pytest.fixture
def db_session(_isolated_app):
    from models import db
    with _isolated_app.app_context():
        yield db.session
    db.session.rollback()


@pytest.fixture
def client(_isolated_app):
    return _isolated_app.test_client()


def _uid():
    return uuid.uuid4().hex[:8]


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


@pytest.fixture
def env(db_session):
    """org owner 用户 + 组织 + 项目 + agent（owner 全部可写）。"""
    from models import Agent, Organization, Project, User

    user = User(username=f"u_{_uid()}", email=f"u_{_uid()}@t.io")
    db_session.add(user)
    db_session.flush()
    org = Organization(name=f"o_{_uid()}", slug=f"o_{_uid()}", owner_id=user.id)
    db_session.add(org)
    db_session.flush()
    agent = Agent(
        name=f"agent_{_uid()}", workspace_id=org.id, owner_id=user.id,
        creator_user_id=user.id, status="ACTIVE", runner_enabled=True,
    )
    db_session.add(agent)
    project = Project(name=f"p_{_uid()}", owner_id=user.id, organization_id=org.id)
    db_session.add(project)
    db_session.commit()
    return {"user": user, "org": org, "agent": agent, "project": project,
            "headers": _headers(user)}


@pytest.fixture
def outsider(db_session):
    """与 env 组织无关的用户（越权边界用）。"""
    from models import User

    user = User(username=f"out_{_uid()}", email=f"out_{_uid()}@t.io")
    db_session.add(user)
    db_session.commit()
    return user


def _mk_mem(db_session, env, title="达梦分页写法", content="达梦用 OFFSET FETCH，不用 LIMIT",
            scope_type="project", scope_id=None, **kw):
    from models import AgentMemory

    import hashlib
    normalized = ' '.join(f'{title}\n{content}'.split()).strip().lower()
    row = AgentMemory(
        organization_id=env["org"].id,
        scope_type=scope_type,
        scope_id=scope_id if scope_id is not None else env["project"].id,
        kind='rule', title=title, content=content,
        dedupe_key=hashlib.sha1(normalized.encode()).hexdigest(),
        confidence=kw.get('confidence', 70),
        human_edited=kw.get('human_edited', 0),
    )
    db_session.add(row)
    db_session.commit()
    return row


# ── 创建与授权矩阵 ────────────────────────────────────────────────────


class TestCreateAuthorization:
    def test_org_owner_creates_project_memory(self, client, env):
        resp = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "project",
            "scope_id": env["project"].id, "kind": "rule",
            "title": "构建必须 linux/amd64", "content": "Apple Silicon 需加平台参数",
        }, headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["human_edited"] == 1
        assert data["source_type"] == "human"
        assert data["scope_label"] == "项目记忆"

    def test_create_dedupes_same_content(self, client, env):
        body = {
            "organization_id": env["org"].id, "scope_type": "agent",
            "scope_id": env["agent"].id, "title": "缓存击穿用单飞",
            "content": "先 singleflight 再落库",
        }
        first = client.post(BASE_URL, json=body, headers=env["headers"])
        second = client.post(BASE_URL, json=body, headers=env["headers"])
        assert first.get_json()["data"]["deduplicated"] is False
        assert second.get_json()["data"]["deduplicated"] is True

    def test_outside_org_gets_forbidden(self, client, env, outsider):
        resp = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "project",
            "scope_id": env["project"].id, "title": "t", "content": "c",
        }, headers=_headers(outsider))
        assert resp.status_code == 403

    def test_non_admin_cannot_write_org_scope(self, client, env, outsider):
        """组织成员（非 owner/admin）不能写组织级记忆：
        outsider 不是成员 → 403（连读都不行）。"""
        resp = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "organization",
            "scope_id": env["org"].id, "title": "t", "content": "c",
        }, headers=_headers(outsider))
        assert resp.status_code == 403

    def test_project_scope_requires_project_manager(self, client, db_session, env):
        """组织里另一个用户（非项目成员）即使能访问组织也写不了该项目记忆。"""
        from models import OrganizationMember, OrganizationMemberStatus, User

        member = User(username=f"m_{_uid()}", email=f"m_{_uid()}@t.io")
        db_session.add(member)
        db_session.flush()
        db_session.add(OrganizationMember(
            organization_id=env["org"].id, user_id=member.id,
            role='MEMBER', status=OrganizationMemberStatus.ACTIVE))
        db_session.commit()

        resp = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "project",
            "scope_id": env["project"].id, "title": "t", "content": "c",
        }, headers=_headers(member))
        assert resp.status_code == 403

        # 但成员可以读
        resp = client.get(BASE_URL, query_string={
            "organization_id": env["org"].id}, headers=_headers(member))
        assert resp.status_code == 200

    def test_agent_scope_owner_ok_other_member_forbidden(self, client, db_session, env):
        from models import OrganizationMember, OrganizationMemberStatus, User

        member = User(username=f"m2_{_uid()}", email=f"m2_{_uid()}@t.io")
        db_session.add(member)
        db_session.flush()
        db_session.add(OrganizationMember(
            organization_id=env["org"].id, user_id=member.id,
            role='MEMBER', status=OrganizationMemberStatus.ACTIVE))
        db_session.commit()

        body = {"organization_id": env["org"].id, "scope_type": "agent",
                "scope_id": env["agent"].id, "title": "t", "content": "c"}
        assert client.post(BASE_URL, json=body, headers=env["headers"]).status_code == 200
        assert client.post(BASE_URL, json=body, headers=_headers(member)).status_code == 403

    def test_user_scope_only_own(self, client, db_session, env, outsider):
        other = outsider
        ok = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "user",
            "scope_id": env["user"].id, "title": "我的偏好", "content": "pytest 风格断言",
        }, headers=env["headers"])
        assert ok.status_code == 200

        forged = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "user",
            "scope_id": env["user"].id, "title": "冒充", "content": "伪造偏好",
        }, headers=_headers(other))
        # outsider 连组织都不可访问 → 403；同组织他人 → 403
        assert forged.status_code == 403

    def test_session_scope_write_rejected(self, client, env):
        resp = client.post(BASE_URL, json={
            "organization_id": env["org"].id, "scope_type": "session",
            "scope_id": 1, "title": "t", "content": "c",
        }, headers=env["headers"])
        assert resp.status_code == 400
        assert resp.get_json()["error_details"]["code"] == "SESSION_SCOPE_SYSTEM_MANAGED"


# ── 列表 / 编辑 / 遗忘 / 召回预览 ─────────────────────────────────────


class TestListEditForget:
    def test_list_filters_and_pagination(self, client, db_session, env):
        _mk_mem(db_session, env, title="第一条", content="达梦相关")
        _mk_mem(db_session, env, title="第二条", content="redis 相关")
        resp = client.get(BASE_URL, query_string={
            "organization_id": env["org"].id, "q": "达梦",
        }, headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["title"] == "第一条"

    def test_edit_marks_human_edited_and_rehashes(self, client, db_session, env):
        row = _mk_mem(db_session, env)
        old_key = row.dedupe_key
        resp = client.put(f"{BASE_URL}/{row.id}", json={
            "content": "更新后的：达梦分页语法是 OFFSET FETCH"}, headers=env["headers"])
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert data["human_edited"] == 1
        assert "更新后的" in data["content"]
        assert data["dedupe_key"] != old_key

    def test_forget_soft_deletes(self, client, db_session, env):
        row = _mk_mem(db_session, env)
        resp = client.delete(f"{BASE_URL}/{row.id}", headers=env["headers"])
        assert resp.status_code == 200
        assert resp.get_json()["data"]["forgotten"] is True

        # 默认列表不再出现；重复遗忘返回 False
        resp = client.get(BASE_URL, query_string={
            "organization_id": env["org"].id}, headers=env["headers"])
        assert resp.get_json()["data"]["total"] == 0
        resp = client.delete(f"{BASE_URL}/{row.id}", headers=env["headers"])
        assert resp.get_json()["data"]["forgotten"] is False

    def test_cross_org_detail_is_404(self, client, db_session, env, outsider):
        row = _mk_mem(db_session, env)
        resp = client.get(f"{BASE_URL}/{row.id}", headers=_headers(outsider))
        assert resp.status_code == 404

    def test_member_cannot_forget_project_memory(self, client, db_session, env):
        """组织普通成员可读，但无项目写权 → 不能遗忘项目记忆。"""
        from models import OrganizationMember, OrganizationMemberStatus, User

        member = User(username=f"m3_{_uid()}", email=f"m3_{_uid()}@t.io")
        db_session.add(member)
        db_session.flush()
        db_session.add(OrganizationMember(
            organization_id=env["org"].id, user_id=member.id,
            role='MEMBER', status=OrganizationMemberStatus.ACTIVE))
        db_session.commit()
        row = _mk_mem(db_session, env)

        resp = client.delete(f"{BASE_URL}/{row.id}", headers=_headers(member))
        assert resp.status_code == 403


class TestRecallPreview:
    def test_preview_returns_labeled_hits(self, client, db_session, env):
        _mk_mem(db_session, env, title="达梦分页写法", content="OFFSET FETCH 语法")
        resp = client.post(f"{BASE_URL}/recall", json={
            "organization_id": env["org"].id,
            "project_id": env["project"].id,
            "query": "达梦分页",
        }, headers=env["headers"])
        assert resp.status_code == 200
        hits = resp.get_json()["data"]["hits"]
        assert hits and hits[0]["scope_label"] == "项目记忆"

    def test_preview_isolated_across_orgs(self, client, db_session, env, outsider):
        _mk_mem(db_session, env, title="机密结论", content="仅 A 组织可见")
        # outsider 自建一个组织作为自己的上下文 → 查不到 A 的记忆
        from models import Organization

        org_b = Organization(name=f"o_{_uid()}", slug=f"o_{_uid()}",
                             owner_id=outsider.id)
        db_session.add(org_b)
        db_session.commit()
        resp = client.post(f"{BASE_URL}/recall", json={
            "organization_id": org_b.id, "query": "机密结论",
        }, headers=_headers(outsider))
        assert resp.status_code == 200
        assert resp.get_json()["data"]["hits"] == []

    def test_recall_prefers_human_edited(self, client, db_session, env):
        auto = _mk_mem(db_session, env, title="缓存方案A", content="互斥锁", confidence=70)
        human = _mk_mem(db_session, env, title="缓存方案B", content="单飞+落库",
                        confidence=70, human_edited=1)
        from services.memory import store as memory_store
        from services.memory.scopes import MemoryScopeRef
        from models import MemoryScopeType

        chain = [MemoryScopeRef(MemoryScopeType.PROJECT, env["project"].id, env["org"].id)]
        hits = memory_store.recall(chain, "缓存方案", top_k=2)
        assert hits[0]['title'] == human.title
        assert hits[1]['title'] == auto.title
