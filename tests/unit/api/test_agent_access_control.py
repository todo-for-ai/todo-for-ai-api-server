"""agent_access_control 纯逻辑权限判定补测（51% → 100% 覆盖缺口）。

所有行驻留会话级库且不使用 conftest factory（user_factory teardown 会
delete 用户，外键置空使驻留 organizations.owner_id 触发 NOT NULL）。
每个用例在单一 app_context 块内完成建行与断言，避免 DetachedInstance。
"""

import uuid

import pytest

from api import agent_access_control as aac


@pytest.fixture
def ws_env(app):
    """每用例独立的 owner + 组织（单 app_context 块内创建，随用例驻留）。"""
    from models import Organization, User
    from werkzeug.security import generate_password_hash

    tag = uuid.uuid4().hex[:8]
    with app.app_context():
        owner = User(username=f"ac-owner-{tag}", email=f"ac-owner-{tag}@example.com")
        owner.password_hash = generate_password_hash("password123")
        org = Organization(name=f"ac-org-{tag}", slug=f"ac-org-{tag}", owner_id=None)
        org.owner = owner
        from models import db

        db.session.add_all([owner, org])
        db.session.commit()
        yield {"owner_id": owner.id, "org_id": org.id}


def _project(db_session, owner_id, org_id):
    from models import Project

    p = Project(name=f"ac-p-{uuid.uuid4().hex[:6]}", status="ACTIVE",
                owner_id=owner_id, organization_id=org_id)
    db_session.add(p)
    db_session.commit()
    return p


def _agent(db_session, workspace_id, creator_user_id, allowed=None):
    from models import Agent

    a = Agent(name=f"ac-agt-{uuid.uuid4().hex[:6]}", workspace_id=workspace_id,
              creator_user_id=creator_user_id, allowed_project_ids=allowed or [])
    db_session.add(a)
    db_session.commit()
    return a


class TestToIntSet:
    def test_empty_and_mixed(self):
        assert aac._to_int_set(None) == set()
        assert aac._to_int_set([]) == set()
        assert aac._to_int_set([1, "2", "x", None]) == {1, 2}
        assert aac._to_int_set("3") == {3}


class TestNormalizeAgentProjectIds:
    def test_empty_allowed_short_circuits_db(self, app, ws_env):
        with app.app_context():
            agent = _agent(app.extensions['sqlalchemy'].session, ws_env["org_id"], ws_env["owner_id"], allowed=[])
            assert aac._normalize_agent_project_ids(agent, ws_env["org_id"]) == set()

    def test_filters_to_workspace_projects(self, app, ws_env):
        with app.app_context():
            from models import db

            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            agent = _agent(db.session, org_id, owner_id, allowed=[987654])
            p = _project(db.session, owner_id, org_id)
            agent.allowed_project_ids = [987654, p.id]
            db.session.commit()
            assert aac._normalize_agent_project_ids(agent, org_id) == {p.id}


class TestResolveWorkspace:
    def test_hit_and_miss(self, app, ws_env):
        with app.app_context():
            assert aac._resolve_workspace(ws_env["org_id"]) is not None
            assert aac._resolve_workspace(987654) is None


class TestUserAccessibleProjects:
    def test_owned_plus_member_union(self, app, ws_env):
        from models import ProjectMember, ProjectMemberRole, ProjectMemberStatus, User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            member = User(username=f"ac-m-{tag}", email=f"ac-m-{tag}@example.com")
            member.password_hash = generate_password_hash("password123")
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            db_session.add(member)
            db_session.commit()
            owned = _project(db_session, owner_id, org_id)
            shared = _project(db_session, owner_id, org_id)
            db_session.add(ProjectMember(
                project_id=shared.id, user_id=member.id,
                role=ProjectMemberRole.MEMBER, status=ProjectMemberStatus.ACTIVE,
            ))
            db_session.commit()
            owner_ref = User.query.get(owner_id)
            member_ref = User.query.get(member.id)
            assert aac._get_user_accessible_project_ids(owner_ref, org_id) >= {owned.id, shared.id}
            assert aac._get_user_accessible_project_ids(member_ref, org_id) == {shared.id}


class TestResolveActorAgent:
    def test_explicit_agent_wins(self, app, ws_env):
        with app.app_context():
            from models import db

            agent = _agent(db.session, ws_env["org_id"], ws_env["owner_id"])
            assert aac._resolve_actor_agent(agent) is agent

    def test_runtime_error_returns_none(self, monkeypatch):
        class G:
            def __getattr__(self, name):
                raise RuntimeError("no app context")
        monkeypatch.setattr(aac, "g", G())
        assert aac._resolve_actor_agent(None) is None

    def test_current_agent_only_when_agent_instance(self, app, ws_env):
        from flask import g

        from models import db

        with app.app_context():
            agent = _agent(db.session, ws_env["org_id"], ws_env["owner_id"])
            g.current_agent = agent
            assert aac._resolve_actor_agent(None) is agent
            g.current_agent = "not-an-agent"
            assert aac._resolve_actor_agent(None) is None


class TestRelationPredicates:
    def test_user_overlap_true_and_false(self, app, ws_env):
        from models import ProjectMember, ProjectMemberRole, ProjectMemberStatus, User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            member = User(username=f"ac-ov-{tag}", email=f"ac-ov-{tag}@example.com")
            member.password_hash = generate_password_hash("password123")
            outsider = User(username=f"ac-out-{tag}", email=f"ac-out-{tag}@example.com")
            outsider.password_hash = generate_password_hash("password123")
            db_session.add_all([member, outsider])
            db_session.commit()
            target = _agent(db_session, org_id, owner_id, allowed=[987001])
            shared = _project(db_session, owner_id, org_id)
            target.allowed_project_ids = [shared.id]
            db_session.add(ProjectMember(
                project_id=shared.id, user_id=member.id,
                role=ProjectMemberRole.MEMBER, status=ProjectMemberStatus.ACTIVE,
            ))
            db_session.commit()

            member_ref = User.query.get(member.id)
            outsider_ref = User.query.get(outsider.id)
            assert aac._user_has_project_overlap(member_ref, target) is True
            assert aac._user_has_project_overlap(outsider_ref, target) is False

    def test_user_same_organization(self, app, ws_env):
        from models import OrganizationMember, OrganizationMemberStatus, User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            member = User(username=f"ac-same-{tag}", email=f"ac-same-{tag}@example.com")
            member.password_hash = generate_password_hash("password123")
            db_session.add(member)
            db_session.commit()
            target = _agent(db_session, org_id, owner_id)
            member_ref = User.query.get(member.id)
            assert aac._user_has_same_organization(member_ref, target) is False
            db_session.add(OrganizationMember(
                organization_id=org_id, user_id=member.id,
                role="MEMBER", status=OrganizationMemberStatus.ACTIVE,
            ))
            db_session.commit()
            member_ref2 = User.query.get(member.id)
            assert aac._user_has_same_organization(member_ref2, target) is True

    def test_agent_relations(self, app, ws_env):
        with app.app_context():
            from models import db

            db_session = db.session
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            shared = _project(db_session, owner_id, org_id)
            actor = _agent(db_session, org_id, owner_id, allowed=[shared.id])
            target = _agent(db_session, org_id, owner_id, allowed=[shared.id, 987654])
            foreign_ws = org_id + 500000
            foreign = _agent(db_session, foreign_ws, owner_id, allowed=[987100])
            assert aac._agent_has_same_organization(actor, target) is True
            assert aac._agent_has_project_overlap(actor, target) is True
            assert aac._agent_has_same_organization(foreign, target) is False
            assert aac._agent_has_project_overlap(foreign, target) is False

    def test_owner_relations(self, app, ws_env):
        from models import User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            owner_ref = User.query.get(owner_id)
            other = User(username=f"ac-o-{tag}", email=f"ac-o-{tag}@example.com")
            other.password_hash = generate_password_hash("password123")
            db_session.add(other)
            db_session.commit()
            owner_agent = _agent(db_session, org_id, owner_id)
            other_agent = _agent(db_session, org_id, other.id)
            assert aac._has_owner_relation_with_user(owner_ref, owner_agent) is True
            assert aac._has_owner_relation_with_user(other, owner_agent) is False
            assert aac._has_owner_relation_with_agent(owner_agent, owner_agent) is True
            assert aac._has_owner_relation_with_agent(other_agent, owner_agent) is False


class TestCanAccessAgentDetail:
    def test_missing_target_false(self):
        assert aac.can_access_agent_detail(None, None) is False

    def test_outsider_denied_owner_allowed(self, app, ws_env):
        from models import User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            outsider = User(username=f"ac-deny-{tag}", email=f"ac-deny-{uuid.uuid4().hex[:8]}@example.com")
            outsider.password_hash = generate_password_hash("password123")
            db_session.add(outsider)
            db_session.commit()
            target = _agent(db_session, org_id, owner_id)
            outsider_ref = User.query.get(outsider.id)
            owner_ref = User.query.get(owner_id)
            assert aac.can_access_agent_detail(outsider_ref, target) is False
            assert aac.can_access_agent_detail(owner_ref, target) is True


class TestEnsureAgentDetailAccess:
    def test_allowed_returns_none(self, app, ws_env):
        with app.app_context():
            from models import db, User

            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            target = _agent(db.session, org_id, owner_id)
            owner = User.query.get(owner_id)
            assert aac.ensure_agent_detail_access(owner, target) is None

    def test_denied_returns_forbidden_response(self, app, ws_env):
        with app.app_context():
            from models import db

            org_id = ws_env["org_id"]
            target = _agent(db.session, org_id, ws_env["owner_id"])
            resp = aac.ensure_agent_detail_access(None, target)
            assert resp is not None


class TestCanAccessBranches:
    """收尾：user 项目重叠分支与 actor_agent 全链分支。"""

    def test_user_overlap_false_when_target_has_no_allowed(self, app, ws_env):
        """target 未配置 allowed_project_ids → overlap 判定直接 False（行 97）。"""
        owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
        with app.app_context():
            from models import User

            outsider = User(username=f"ac-o-{uuid.uuid4().hex[:8]}", email=f"ac-o2-{uuid.uuid4().hex[:8]}@example.com")
            outsider.password_hash = generate_password_hash = __import__('werkzeug.security', fromlist=['generate_password_hash']).generate_password_hash("x")
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            db_session.add(outsider)
            db_session.commit()
            outsider_id = outsider.id
            target = _agent(db_session, org_id, owner_id, allowed=[])
            target_id = target.id
        with app.app_context():
            outsider_ref = __import__('models').User.query.get(outsider_id)
            target_ref = __import__('models').Agent.query.get(target_id)
            assert aac.can_access_agent_detail(outsider_ref, target_ref) is False

    def test_agent_actor_overlap_false_when_target_has_no_allowed(self, app, ws_env):
        """行 108：actor_agent 存在但 target 无 allowed → overlap False。"""
        with app.app_context():
            from models import db

            db_session = db.session
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            # 不同 workspace、不同 creator：owner relation / same-org 都不成立，
            # 走到 _agent_has_project_overlap（target 无 allowed → False）
            actor = _agent(db_session, org_id + 1, owner_id + 500000, allowed=[987654])
            target = _agent(db_session, org_id, owner_id, allowed=[])
            assert aac.can_access_agent_detail(None, target, actor_agent=actor) is False

    def test_agent_actor_full_chain_via_g(self, app, ws_env):
        """行 143-148：g.current_agent 供角色链——同组织/项目重叠返回 True。"""
        from flask import g

        from models import db

        with app.app_context():
            from models import db

            db_session = db.session
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            actor = _agent(db_session, org_id, owner_id, allowed=[987654])
            target = _agent(db_session, org_id, owner_id, allowed=[987655])
            g.current_agent = actor
            assert aac.can_access_agent_detail(None, target) is True
            g.current_agent = "not-an-agent"
            assert aac.can_access_agent_detail(None, target) is False


class TestCanAccessTailBranches:
    """收尾：user 项目重叠 False、actor 同组织 True、跨组织项目重叠 True。"""

    def test_user_overlap_false_via_outsider(self, app, ws_env):
        """行 97：user 可访问集与 target allowed 无交集 → False。"""
        from models import User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            outsider = User(username=f"ac-uof-{tag}", email=f"ac-uof-{tag}@example.com")
            outsider.password_hash = generate_password_hash("password123")
            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            db_session.add(outsider)
            db_session.commit()
            outsider_id = outsider.id
            shared = _project(db_session, owner_id, org_id)
            target = _agent(db_session, org_id, owner_id, allowed=[shared.id])
            target_id = target.id
        with app.app_context():
            from models import Agent, User

            outsider_ref = User.query.get(outsider_id)
            target_ref = Agent.query.get(target_id)
            assert aac._user_has_project_overlap(outsider_ref, target_ref) is False

    def test_agent_actor_same_organization_true(self, app, ws_env):
        """行 146：actor 与 target 同 workspace（creator 不同）→ True。"""
        with app.app_context():
            from models import db, User
            from werkzeug.security import generate_password_hash

            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            other = User(username=f"ac-o2-{uuid.uuid4().hex[:8]}", email=f"ac-o2-{uuid.uuid4().hex[:8]}@example.com")
            other.password_hash = generate_password_hash("password123")
            db_session.add(other)
            db_session.commit()
            actor = _agent(db_session, org_id, other.id, allowed=[987654])
            target = _agent(db_session, org_id, owner_id, allowed=[987655])
            assert aac.can_access_agent_detail(None, target, actor_agent=actor) is True

    def test_agent_actor_cross_organization_project_overlap_true(self, app, ws_env):
        """行 148：跨 workspace 但项目 allowed 交集非空 → True。"""
        with app.app_context():
            from models import db, Organization, User
            from werkzeug.security import generate_password_hash

            db_session = __import__('flask').current_app.extensions['sqlalchemy'].session
            tag = uuid.uuid4().hex[:8]
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            other_org_user = User(username=f"ac-xo-{tag}", email=f"ac-xo-{tag}@example.com")
            other_org_user.password_hash = generate_password_hash("password123")
            db_session.add(other_org_user)
            db_session.commit()
            shared = _project(db_session, owner_id, org_id)
            target = _agent(db_session, org_id, owner_id, allowed=[shared.id])
            foreign_org = Organization(name=f"ac-fo-{tag}", slug=f"ac-fo-{tag}", owner_id=other_org_user.id)
            db_session.add(foreign_org)
            db_session.commit()
            actor = _agent(db_session, foreign_org.id, other_org_user.id, allowed=[shared.id])
            assert aac.can_access_agent_detail(None, target, actor_agent=actor) is True


class TestCanAccessFinal:
    """收口最后 3 行：member 交集 True / same_org 悬空 workspace False。"""

    def test_member_user_project_overlap_true(self, app, db_session, ws_env):
        """非 owner 成员 + target allowed 含其项目 → overlap True（138）。"""
        from models import ProjectMember, ProjectMemberRole, ProjectMemberStatus, User

        with app.app_context():
            owner_id, org_id = ws_env["owner_id"], ws_env["org_id"]
            member = User(username=f"ac-fi-m-{uuid.uuid4().hex[:8]}", email=f"fi-m-{uuid.uuid4().hex[:8]}@example.com")
            from werkzeug.security import generate_password_hash
            member.password_hash = generate_password_hash("password123")
            db_session.add(member)
            db_session.commit()
            shared = _project(db_session, owner_id, org_id)
            target = _agent(db_session, org_id, owner_id, allowed=[shared.id])
            db_session.add(ProjectMember(
                project_id=shared.id, user_id=member.id,
                role=ProjectMemberRole.MEMBER, status=ProjectMemberStatus.ACTIVE,
            ))
            db_session.commit()
            member_ref = User.query.get(member.id)
            assert aac.can_access_agent_detail(member_ref, target) is True

    def test_user_same_org_false_when_workspace_dangling(self, app, db_session, ws_env):
        """target.workspace_id 指向不存在的组织 → same_org False（行 97）。"""
        with app.app_context():
            from models import User
            from werkzeug.security import generate_password_hash

            dangling_ws = ws_env["org_id"] + 400000
            target = _agent(db_session, dangling_ws, ws_env["owner_id"])
            tag = uuid.uuid4().hex[:8]
            member = User(username=f"ac-m2-{tag}", email=f"ac-m2-{tag}@example.com")
            member.password_hash = generate_password_hash("password123")
            db_session.add(member)
            db_session.commit()
            assert aac._user_has_same_organization(member, target) is False


class TestOwnerRelationLine:
    def test_owner_user_relation_true(self, app, db_session, ws_env):
        from models import User

        with app.app_context():
            target = _agent(db_session, ws_env["org_id"], ws_env["owner_id"])
            owner = User.query.get(ws_env["owner_id"])
            assert aac.can_access_agent_detail(owner, target) is True


class TestSameOrgUserBranch:
    def test_org_member_user_hits_same_org_branch(self, app, db_session, ws_env):
        from models import OrganizationMember, OrganizationMemberStatus, User
        from werkzeug.security import generate_password_hash

        tag = uuid.uuid4().hex[:8]
        with app.app_context():
            member = User(username=f"ac-so-{tag}", email=f"ac-so-{tag}@example.com")
            member.password_hash = generate_password_hash("password123")
            db_session.add(member)
            db_session.commit()
            target = _agent(db_session, ws_env["org_id"], ws_env["owner_id"])
            db_session.add(OrganizationMember(
                organization_id=ws_env["org_id"], user_id=member.id,
                role="MEMBER", status=OrganizationMemberStatus.ACTIVE,
            ))
            db_session.commit()
            member_ref = User.query.get(member.id)
            assert aac.can_access_agent_detail(member_ref, target) is True
