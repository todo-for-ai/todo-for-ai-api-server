"""岗位角色分类数据集（role_taxonomy）与行业过滤回归测试。"""

import importlib.util
import os
import uuid

import pytest

from app import create_app
from models import db

BASE_URL = "/todo-for-ai/api/v1"


def _load_taxonomy():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(root, "scripts", "role_taxonomy.py")
    spec = importlib.util.spec_from_file_location("role_taxonomy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def taxonomy():
    return _load_taxonomy()


@pytest.fixture(scope="function", autouse=True)
def _isolated_app():
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


@pytest.fixture
def workspace_env(_isolated_app):
    from flask_jwt_extended import create_access_token
    from models import Organization, User

    user = User(username=f"u_{uuid.uuid4().hex[:8]}", email=f"u_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(user)
    db.session.flush()
    org = Organization(name=f"o_{uuid.uuid4().hex[:8]}", slug=f"o_{uuid.uuid4().hex[:8]}", owner_id=user.id)
    db.session.add(org)
    db.session.commit()
    token = create_access_token(identity=str(user.id))
    return {"org": org, "user": user, "headers": {"Authorization": f"Bearer {token}"}}


class TestRoleTaxonomyDataset:
    def test_scale_industries(self, taxonomy):
        assert len(taxonomy.INDUSTRIES) >= 100

    def test_scale_roles(self, taxonomy):
        roles = taxonomy.build_expanded()
        assert len(roles) >= 5000
        assert len(roles) <= 10000

    def test_names_unique_and_idempotent(self, taxonomy):
        roles = taxonomy.build_expanded()
        names = [r["name"] for r in roles]
        assert len(names) == len(set(names))
        again = taxonomy.build_expanded()
        assert [r["name"] for r in again] == names

    def test_role_fields_complete(self, taxonomy):
        roles = taxonomy.build_expanded()
        sample = roles[0]
        for field in ("name", "display_name", "industry", "category", "description", "capability_tags", "skills"):
            assert sample.get(field), field

    def test_system_prompt_generation(self, taxonomy):
        prompt = taxonomy.build_system_prompt("口腔执业医师", "口腔诊所", "种植手术,正畸方案", "custom")
        assert "口腔诊所" in prompt and "口腔执业医师" in prompt and "种植手术" in prompt


class TestRoleTemplateIndustryFilter:
    def _make_template(self, name, display, industry):
        from models import AgentRoleTemplate, AgentRoleTemplateStatus, User

        creator = User.query.first() or User(username="sys", email="sys@t.io")
        if creator.id is None:
            db.session.add(creator)
            db.session.flush()
        t = AgentRoleTemplate(
            workspace_id=None,
            created_by_user_id=creator.id,
            name=name,
            display_name=display,
            industry=industry,
            is_builtin=True,
            status=AgentRoleTemplateStatus.ACTIVE,
        )
        db.session.add(t)
        db.session.commit()
        return t

    def test_list_filter_by_industry(self, client, workspace_env):
        from sqlalchemy import select
        from models import User
        first_user = db.session.scalar(select(User).order_by(User.id))
        self._make_template("t-a", "口腔执业医师", "口腔诊所")
        self._make_template("t-b", "大田种植技术员", "大田种植")
        _ = first_user

        resp = client.get(
            f"{BASE_URL}/workspaces/{workspace_env['org'].id}/agent-role-templates?industry=口腔诊所",
            headers=workspace_env["headers"],
        )
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        assert items and all(i["industry"] == "口腔诊所" for i in items)

    def test_industries_endpoint(self, client, workspace_env):
        self._make_template("t-a2", "口腔执业医师", "口腔诊所")
        self._make_template("t-b2", "种植技术员", "大田种植")

        resp = client.get(
            f"{BASE_URL}/workspaces/{workspace_env['org'].id}/agent-role-templates/industries",
            headers=workspace_env["headers"],
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        industries = {i["industry"] for i in data["industries"]}
        assert {"口腔诊所", "大田种植"} <= industries
        assert data["total"] >= 2
