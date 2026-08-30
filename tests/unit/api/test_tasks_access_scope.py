"""Tests for task access scope query builder."""

from api.tasks.routes_tasks import _build_accessible_tasks_query
from models import ProjectMember, ProjectMemberRole, ProjectMemberStatus


def test_build_accessible_tasks_query_owner_only_fast_path(db_session, user_factory, project_factory, task_factory):
    """Owner-only users should query tasks by owner_id without project-membership subquery."""
    owner = user_factory()
    own_project = project_factory(owner_id=owner.id)
    own_task = task_factory(project_id=own_project.id, owner_id=owner.id)

    query = _build_accessible_tasks_query(owner.id)
    sql = str(query.statement.compile(compile_kwargs={"literal_binds": True})).lower()

    assert "tasks.owner_id" in sql
    assert "project_members" not in sql
    task_ids = [task.id for task in query.all()]
    assert own_task.id in task_ids


def test_build_accessible_tasks_query_includes_foreign_member_projects(db_session, user_factory, project_factory, task_factory):
    """Users should see both owned tasks and tasks from projects they actively belong to."""
    owner = user_factory()
    member_user = user_factory()
    outsider_owner = user_factory()

    owned_project = project_factory(owner_id=member_user.id)
    foreign_member_project = project_factory(owner_id=owner.id)
    outsider_project = project_factory(owner_id=outsider_owner.id)

    owned_task = task_factory(project_id=owned_project.id, owner_id=member_user.id)
    foreign_member_task = task_factory(project_id=foreign_member_project.id, owner_id=owner.id)
    outsider_task = task_factory(project_id=outsider_project.id, owner_id=outsider_owner.id)

    membership = ProjectMember(
        project_id=foreign_member_project.id,
        user_id=member_user.id,
        role=ProjectMemberRole.MEMBER,
        status=ProjectMemberStatus.ACTIVE,
        invited_by=owner.id,
    )
    db_session.add(membership)
    db_session.commit()

    query = _build_accessible_tasks_query(member_user.id)
    task_ids = {task.id for task in query.all()}

    assert owned_task.id in task_ids
    assert foreign_member_task.id in task_ids
    assert outsider_task.id not in task_ids

