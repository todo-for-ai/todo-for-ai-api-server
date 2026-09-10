"""逾期任务优先级自动升级（api/agents/task_escalation.py）回归测试：
阶梯逐级上调 / 通知 / 截止与状态过滤 / owner 作用域 / 提交分支，到行。

Task 用独立高位 id 段（9_200_000+），用例结束清场。
"""

import datetime as dt
import itertools
import uuid

import pytest

BASE_URL = "/todo-for-ai/api/v1"

_TASK_ID = itertools.count(9_200_001)


@pytest.fixture(autouse=True)
def _clean_escalation_tables(db_session):
    from models import Notification, Task
    db_session.query(Notification).delete(synchronize_session=False)
    db_session.query(Task).filter(Task.id >= 9_200_000).delete(
        synchronize_session=False)
    db_session.commit()
    yield
    db_session.query(Notification).delete(synchronize_session=False)
    db_session.query(Task).filter(Task.id >= 9_200_000).delete(
        synchronize_session=False)
    db_session.commit()


def _headers(user):
    from flask_jwt_extended import create_access_token
    return {"Authorization": f"Bearer {create_access_token(identity=str(user.id))}"}


def _now(offset_days=0.0):
    return dt.datetime.utcnow() - dt.timedelta(days=offset_days)


def _make_user(db_session):
    from models import User
    user = User(username=f"te_{uuid.uuid4().hex[:8]}",
                email=f"te_{uuid.uuid4().hex[:6]}@t.io")
    db_session.add(user)
    db_session.commit()
    return user


def _make_project(db_session, user):
    from models import Project
    project = Project(name=f"tp_{uuid.uuid4().hex[:8]}", owner_id=user.id,
                      status="ACTIVE")
    db_session.add(project)
    db_session.commit()
    return project


def _make_task(db_session, project, priority="LOW", status="TODO",
               due_days_ago=2, with_due=True):
    from models import Task, TaskPriority
    task = Task(id=next(_TASK_ID), title=f"tk_{uuid.uuid4().hex[:8]}",
                content="x", project_id=project.id, owner_id=project.owner_id,
                status=status, priority=TaskPriority[priority.upper()],
                due_date=(_now(due_days_ago) if with_due else None),
                is_ai_task=False)
    db_session.add(task)
    db_session.commit()
    return task


# ────────────────────────── 服务级 ──────────────────────────

def test_escalates_overdue_low_to_medium_with_notification(db_session):
    from api.agents.task_escalation import escalate_overdue_tasks
    from models import Notification, TaskPriority

    user = _make_user(db_session)
    project = _make_project(db_session, user)
    task = _make_task(db_session, project, priority="LOW")

    escalated = escalate_overdue_tasks(owner_id=user.id, overdue_after_days=1)

    assert escalated == [task.id]
    db_session.expire_all()
    assert task.priority == TaskPriority.MEDIUM
    note = Notification.query.filter_by(task_id=task.id,
                                        event_type="task_priority_escalated").one()
    assert note.payload["old_priority"] == "low"
    assert note.payload["new_priority"] == "medium"
    assert note.user_id == user.id


def test_ladder_climbs_one_level_per_call(db_session):
    from api.agents.task_escalation import escalate_overdue_tasks
    from models import TaskPriority

    user = _make_user(db_session)
    project = _make_project(db_session, user)
    task = _make_task(db_session, project, priority="medium")

    escalate_overdue_tasks(owner_id=user.id)
    db_session.expire_all()
    assert task.priority == TaskPriority.HIGH

    escalate_overdue_tasks(owner_id=user.id)
    db_session.expire_all()
    assert task.priority == TaskPriority.URGENT

    # urgent 已到顶：查询直接排除，不再变化
    escalated = escalate_overdue_tasks(owner_id=user.id)
    assert escalated == []


def test_urgent_done_cancelled_and_null_due_excluded(db_session):
    from api.agents.task_escalation import escalate_overdue_tasks
    user = _make_user(db_session)
    project = _make_project(db_session, user)
    urgent = _make_task(db_session, project, priority="URGENT")
    done = _make_task(db_session, project, priority="low", status="DONE")
    cancelled = _make_task(db_session, project, priority="low", status="CANCELLED")
    no_due = _make_task(db_session, project, priority="low", with_due=False)

    assert escalate_overdue_tasks(owner_id=user.id) == []
    from models import TaskPriority
    for t in (urgent, done, cancelled, no_due):
        db_session.expire(t)
        assert t.priority != TaskPriority.HIGH  # 均未被升级


def test_overdue_after_days_cutoff(db_session):
    from api.agents.task_escalation import escalate_overdue_tasks
    user = _make_user(db_session)
    project = _make_project(db_session, user)
    # 逾期 2 天：overdue_after_days=1 时即算逾期
    fresh = _make_task(db_session, project, priority="low", due_days_ago=2)
    assert escalate_overdue_tasks(owner_id=user.id, overdue_after_days=1) == [fresh.id]

    db_session.expire_all()
    fresh.priority = "low"
    db_session.commit()
    # 但 overdue_after_days=3 时：cutoff=3 天前，逾期 2 天不够格
    assert escalate_overdue_tasks(owner_id=user.id, overdue_after_days=3) == []


def test_owner_filter_scopes_projects(db_session):
    from api.agents.task_escalation import escalate_overdue_tasks
    owner = _make_user(db_session)
    stranger = _make_user(db_session)
    p_owner = _make_project(db_session, owner)
    p_stranger = _make_project(db_session, stranger)
    mine = _make_task(db_session, p_owner, priority="LOW")
    theirs = _make_task(db_session, p_stranger, priority="LOW")

    escalated = escalate_overdue_tasks(owner_id=owner.id)
    assert escalated == [mine.id]
    db_session.expire(theirs)
    from models import TaskPriority
    assert theirs.priority == TaskPriority.LOW  # 别人的未被波及


def test_owner_id_none_escalates_all_owners(db_session):
    from api.agents.task_escalation import escalate_overdue_tasks
    u1 = _make_user(db_session)
    u2 = _make_user(db_session)
    p1 = _make_project(db_session, u1)
    p2 = _make_project(db_session, u2)
    t1 = _make_task(db_session, p1, priority="LOW")
    t2 = _make_task(db_session, p2, priority="LOW")

    escalated = escalate_overdue_tasks(owner_id=None)
    assert sorted(escalated) == sorted([t1.id, t2.id])


def test_no_commit_when_nothing_escalated(db_session):
    """无升级时不提交分支（保持与有升级时同一路径覆盖）。"""
    from api.agents.task_escalation import escalate_overdue_tasks
    user = _make_user(db_session)
    project = _make_project(db_session, user)
    _make_task(db_session, project, priority="low", due_days_ago=-5)  # 未来截止

    assert escalate_overdue_tasks(owner_id=user.id) == []


# ────────────────────────── 路由级（潜伏 NameError 修复回归） ──────────────────────────

def test_maintenance_escalate_endpoint_works(client, db_session):
    """回归：maintenance.py 三处调用此前未导入 _escalate_overdue_tasks（必 500）。"""
    user = _make_user(db_session)
    project = _make_project(db_session, user)
    _make_task(db_session, project, priority="LOW")

    resp = client.post(f"{BASE_URL}/agents/maintenance/escalate-overdue",
                       json={"overdue_after_days": 1}, headers=_headers(user))
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["escalated_count"] == 1
