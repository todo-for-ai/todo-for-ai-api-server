"""
Agent 详情访问权限控制
"""

from typing import Optional, Set

from flask import g
from models import Agent, Organization, Project, ProjectMember, ProjectMemberStatus, User, db
from .base import ApiResponse


def _to_int_set(raw_values) -> Set[int]:
    if not raw_values:
        return set()

    values = raw_values if isinstance(raw_values, list) else [raw_values]
    result: Set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _normalize_agent_project_ids(agent: Agent, workspace_id: int) -> Set[int]:
    project_ids = _to_int_set(agent.allowed_project_ids)
    if not project_ids:
        return set()

    rows = (
        db.session.query(Project.id)
        .filter(
            Project.organization_id == workspace_id,
            Project.id.in_(list(project_ids)),
        )
        .all()
    )
    return {int(row.id) for row in rows}


def _resolve_workspace(workspace_id: int) -> Optional[Organization]:
    return Organization.query.get(workspace_id)


def _get_user_accessible_project_ids(user: User, workspace_id: int) -> Set[int]:
    owned_rows = (
        db.session.query(Project.id)
        .filter(
            Project.owner_id == user.id,
            Project.organization_id == workspace_id,
        )
        .all()
    )
    member_rows = (
        db.session.query(ProjectMember.project_id)
        .join(Project, Project.id == ProjectMember.project_id)
        .filter(
            ProjectMember.user_id == user.id,
            ProjectMember.status == ProjectMemberStatus.ACTIVE,
            Project.organization_id == workspace_id,
        )
        .all()
    )

    result: Set[int] = set()
    for row in owned_rows:
        result.add(int(row.id))
    for row in member_rows:
        result.add(int(row.project_id))
    return result


def _resolve_actor_agent(explicit_actor_agent: Optional[Agent]) -> Optional[Agent]:
    if explicit_actor_agent is not None:
        return explicit_actor_agent
    try:
        candidate = getattr(g, 'current_agent', None)
    except RuntimeError:
        # 调用方可能在请求上下文之外执行（例如离线脚本）
        return None
    return candidate if isinstance(candidate, Agent) else None


def _user_has_project_overlap(user: User, target_agent: Agent) -> bool:
    target_project_ids = _normalize_agent_project_ids(target_agent, target_agent.workspace_id)
    if not target_project_ids:
        return False

    user_project_ids = _get_user_accessible_project_ids(user, target_agent.workspace_id)
    return bool(target_project_ids.intersection(user_project_ids))


def _user_has_same_organization(user: User, target_agent: Agent) -> bool:
    workspace = target_agent.workspace or _resolve_workspace(target_agent.workspace_id)
    if not workspace:
        return False
    return bool(user.can_access_organization(workspace))


def _agent_has_same_organization(actor_agent: Agent, target_agent: Agent) -> bool:
    return int(actor_agent.workspace_id) == int(target_agent.workspace_id)


def _agent_has_project_overlap(actor_agent: Agent, target_agent: Agent) -> bool:
    target_project_ids = _normalize_agent_project_ids(target_agent, target_agent.workspace_id)
    if not target_project_ids:
        return False

    actor_project_ids = _normalize_agent_project_ids(actor_agent, target_agent.workspace_id)
    return bool(target_project_ids.intersection(actor_project_ids))


def _has_owner_relation_with_user(user: User, target_agent: Agent) -> bool:
    return int(target_agent.creator_user_id) == int(user.id)


def _has_owner_relation_with_agent(actor_agent: Agent, target_agent: Agent) -> bool:
    if int(actor_agent.id) == int(target_agent.id):
        return True
    return int(actor_agent.creator_user_id) == int(target_agent.creator_user_id)


def can_access_agent_detail(
    actor_user: Optional[User],
    target_agent: Agent,
    actor_agent: Optional[Agent] = None,
) -> bool:
    if not target_agent:
        return False

    resolved_actor_agent = _resolve_actor_agent(actor_agent)

    if actor_user:
        if _has_owner_relation_with_user(actor_user, target_agent):
            return True
        if _user_has_same_organization(actor_user, target_agent):
            return True
        if _user_has_project_overlap(actor_user, target_agent):
            return True

    if resolved_actor_agent:
        if _has_owner_relation_with_agent(resolved_actor_agent, target_agent):
            return True
        if _agent_has_same_organization(resolved_actor_agent, target_agent):
            return True
        if _agent_has_project_overlap(resolved_actor_agent, target_agent):
            return True

    return False


def ensure_agent_detail_access(
    actor_user: Optional[User],
    target_agent: Agent,
    actor_agent: Optional[Agent] = None,
):
    if can_access_agent_detail(actor_user=actor_user, target_agent=target_agent, actor_agent=actor_agent):
        return None
    return ApiResponse.forbidden('Access denied').to_response()
