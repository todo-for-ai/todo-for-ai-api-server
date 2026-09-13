"""任务图（DAG）：写侧防环 + 读侧 task-graph 端点 + 环检测服务单测。

有向图语义：blocked_by 边 blocker → task（阻塞者先完成，下游才派发——
依赖门在 agent_runtime_pull）。环 = 互相等待、永久无法派发：
- 写侧 PUT /tasks/<id>/dependencies 拒绝自依赖与传递成环；
- 规划侧（goal_decomposition）丢弃成环边（见 test_goal_decomposition）；
- 读侧 GET /tasks/projects/<id>/task-graph 暴露节点/边/就绪态/环组。
隔离方式与 test_tasks_remaining_api.py 一致：独立 in-memory SQLite +
用户 JWT。
"""

import uuid

import pytest
from flask_jwt_extended import create_access_token

from app import create_app
from models import (
    Project,
    ProjectMember,
    ProjectMemberStatus,
    Task,
    TaskStatus,
    User,
    db,
)

BASE_URL = "/todo-for-ai/api/v1/tasks"
_ID_SEQ = iter(range(82_000_001, 82_100_000))


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
def env(client):
    owner = User(username=f"tg_{uuid.uuid4().hex[:8]}",
                 email=f"tg_{uuid.uuid4().hex[:6]}@t.io")
    db.session.add(owner)
    db.session.flush()
    project = Project(name=f"p_{uuid.uuid4().hex[:6]}", owner_id=owner.id)
    db.session.add(project)
    db.session.commit()
    return {
        "owner": owner,
        "project": project,
        "headers": {
            "Authorization": f"Bearer {create_access_token(identity=str(owner.id))}"
        },
    }


def _mk_task(env, status="todo", blocked_by=None, title=None):
    task = Task(
        title=title or f"t_{uuid.uuid4().hex[:6]}",
        content='{"prompt":"x"}',
        project_id=env["project"].id,
        owner_id=env["owner"].id,
        is_ai_task=True,
        status=TaskStatus(status),
        blocked_by_task_ids=blocked_by or [],
        dod=[],
    )
    db.session.add(task)
    db.session.commit()
    return task


def _put_deps(client, env, task_id, blocked_by):
    return client.put(
        f"{BASE_URL}/{task_id}/dependencies",
        headers=env["headers"],
        json={"blocking_task_ids": [], "blocked_by_task_ids": blocked_by},
    )


def _get_graph(client, env):
    return client.get(
        f"{BASE_URL}/projects/{env['project'].id}/task-graph",
        headers=env["headers"],
    )


# ── 写侧防环 ──────────────────────────────────────────────────────────


class TestDependencyCycleRejection:
    def test_self_dependency_rejected(self, client, env):
        task = _mk_task(env)
        resp = _put_deps(client, env, task.id, [task.id])
        assert resp.status_code == 400
        assert "cycle" in resp.get_json()["message"]

    def test_direct_two_node_cycle_rejected(self, client, env):
        a = _mk_task(env)
        b = _mk_task(env)
        assert _put_deps(client, env, a.id, [b.id]).status_code == 200
        # 此时边 B→A；再让 B 依赖 A（新边 A→B）即闭合 A↔B
        resp = _put_deps(client, env, b.id, [a.id])
        assert resp.status_code == 400
        assert "cycle" in resp.get_json()["message"]
        # 被拒的编辑不得落库
        db.session.expire_all()
        assert b.blocked_by_task_ids == []

    def test_transitive_cycle_rejected(self, client, env):
        a = _mk_task(env)
        b = _mk_task(env)
        c = _mk_task(env)
        assert _put_deps(client, env, a.id, [b.id]).status_code == 200
        assert _put_deps(client, env, b.id, [c.id]).status_code == 200
        # 链：C→B→A；再让 C 依赖 A（新边 A→C）闭合三节点环
        resp = _put_deps(client, env, c.id, [a.id])
        assert resp.status_code == 400

    def test_benign_chain_still_accepted(self, client, env):
        a = _mk_task(env)
        b = _mk_task(env)
        c = _mk_task(env)
        assert _put_deps(client, env, a.id, [b.id]).status_code == 200
        assert _put_deps(client, env, b.id, [c.id]).status_code == 200
        db.session.expire_all()
        assert a.blocked_by_task_ids == [b.id]
        assert b.blocked_by_task_ids == [c.id]

    def test_dirty_entries_tolerated_in_validation(self, client, env):
        # 脏条目（非数字）按归一化口径忽略，不误判成环也不 500
        a = _mk_task(env)
        b = _mk_task(env)
        resp = _put_deps(client, env, a.id, ["abc", None, "", b.id])
        assert resp.status_code == 200
        db.session.expire_all()
        assert a.blocked_by_task_ids == ["abc", None, "", b.id]


# ── 读侧 task-graph 端点 ──────────────────────────────────────────────


class TestTaskGraphEndpoint:
    def test_unknown_project_404(self, client, env):
        resp = client.get(f"{BASE_URL}/projects/999999/task-graph",
                          headers=env["headers"])
        assert resp.status_code == 404

    def test_non_member_403(self, client, env):
        outsider = User(username=f"out_{uuid.uuid4().hex[:8]}",
                        email=f"out_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(outsider)
        db.session.commit()
        resp = client.get(
            f"{BASE_URL}/projects/{env['project'].id}/task-graph",
            headers={"Authorization": f"Bearer {create_access_token(identity=str(outsider.id))}"},
        )
        assert resp.status_code == 403

    def test_active_member_200(self, client, env):
        member = User(username=f"m_{uuid.uuid4().hex[:8]}",
                      email=f"m_{uuid.uuid4().hex[:6]}@t.io")
        db.session.add(member)
        db.session.flush()
        db.session.add(ProjectMember(
            project_id=env["project"].id, user_id=member.id,
            status=ProjectMemberStatus.ACTIVE,
        ))
        db.session.commit()
        resp = client.get(
            f"{BASE_URL}/projects/{env['project'].id}/task-graph",
            headers={"Authorization": f"Bearer {create_access_token(identity=str(member.id))}"},
        )
        assert resp.status_code == 200

    def test_graph_shape_and_readiness(self, client, env):
        done = _mk_task(env, status="done")
        cancelled = _mk_task(env, status="cancelled")
        ready = _mk_task(env, status="todo")
        blocked = _mk_task(env, status="todo", blocked_by=[done.id, cancelled.id])
        held = _mk_task(env, status="in_progress")  # 阻塞者未终态
        waiting = _mk_task(env, status="todo", blocked_by=[held.id])

        data = _get_graph(client, env).get_json()["data"]
        by_id = {n["id"]: n for n in data["nodes"]}

        assert by_id[done.id]["readiness"] == "done"
        assert by_id[cancelled.id]["readiness"] == "cancelled"
        assert by_id[ready.id]["readiness"] == "ready"
        # 依赖全部到终态 → ready（可派发）
        assert by_id[blocked.id]["readiness"] == "ready"
        # 阻塞者 in_progress 未终态 → blocked，且 unresolved_blockers 指向它
        assert by_id[waiting.id]["readiness"] == "blocked"
        assert by_id[waiting.id]["unresolved_blockers"] == [held.id]

        edge = next(e for e in data["edges"] if e["to"] == waiting.id)
        assert edge["from"] == held.id

        assert data["stats"]["total"] == 6
        assert data["stats"]["done"] == 1
        assert data["stats"]["cancelled"] == 1
        assert data["stats"]["blocked"] == 1
        assert data["stats"]["ready"] == 3
        assert data["cycles"] == []
        assert data["truncated"] is False

    def test_external_blocker_status_resolved(self, client, env):
        # 跨项目引用同样计入就绪态：外部任务未完成 → blocked
        external = Task(
            title="ext", project_id=env["project"].id + 1,
            owner_id=env["owner"].id, is_ai_task=True,
            status=TaskStatus.TODO, dod=[],
        )
        db.session.add(external)
        db.session.commit()
        dependent = _mk_task(env, blocked_by=[external.id])

        data = _get_graph(client, env).get_json()["data"]
        by_id = {n["id"]: n for n in data["nodes"]}
        assert by_id[dependent.id]["readiness"] == "blocked"
        # 外部节点不入图（不属本项目），但边因缺头节点也不成立
        assert all(e["from"] != external.id for e in data["edges"])

    def test_ghost_blocker_treated_as_satisfied(self, client, env):
        dependent = _mk_task(env, blocked_by=[987654321])
        data = _get_graph(client, env).get_json()["data"]
        by_id = {n["id"]: n for n in data["nodes"]}
        assert by_id[dependent.id]["readiness"] == "ready"

    def test_cycle_group_reported(self, client, env):
        # 绕过写侧防环直接造环（如历史脏数据），读侧必须可见
        a = _mk_task(env)
        b = _mk_task(env)
        a.blocked_by_task_ids = [b.id]
        b.blocked_by_task_ids = [a.id]
        db.session.commit()

        data = _get_graph(client, env).get_json()["data"]
        assert data["cycles"] == [sorted([a.id, b.id])]


# ── 环检测服务单测 ────────────────────────────────────────────────────


class TestFindDependencyCycle:
    def test_direction_semantics(self, env):
        from services.task_graph import find_dependency_cycle
        a = _mk_task(env)
        b = _mk_task(env)
        # 边 B→A（A 依赖 B）。让 B 依赖 A（新边 A→B）= 成环：
        # 从新阻塞者 A 沿 blocked_by 链能走回 B。
        a.blocked_by_task_ids = [b.id]
        db.session.commit()
        assert find_dependency_cycle(b.id, [a.id]) == b.id
        # 反方向不成立：让 A 再依赖 C（新边 C→A），C 的前置链够不到 A
        c = _mk_task(env)
        assert find_dependency_cycle(a.id, [c.id]) is None

    def test_self_and_normalization(self, env):
        from services.task_graph import find_dependency_cycle, normalize_dependency_ids
        a = _mk_task(env)
        assert find_dependency_cycle(a.id, [a.id]) == a.id
        assert find_dependency_cycle(a.id, ["x", None, a.id]) == a.id
        assert normalize_dependency_ids(["1", 1, "x", None, 2]) == [1, 2]

    def test_cyclic_groups_self_loop(self):
        from services.task_graph import cyclic_groups
        groups = cyclic_groups({1: [2], 2: [1], 3: [3], 4: []})
        assert sorted(sorted(g) for g in groups) == [[1, 2], [3]]
