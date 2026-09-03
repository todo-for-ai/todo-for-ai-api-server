"""Project overview aggregation routes.

项目详情页的跨域聚合端点：把组织归属、repo 绑定、项目可见 Agent 及其
在本项目上的运行画像、进行中租约、本项目相关审计事件聚合为一次请求。

背景：这些能力分散在 workspace/agent/task 各作用域的端点里，前端此前
只能多请求拼接 + 客户端过滤；审计/审批没有项目级服务端过滤。本端点
按 project_id 在服务端聚合，webpage 的 projectContext 优先消费它。
"""

from datetime import datetime, timedelta

from sqlalchemy import func

from models import (
    db,
    Project,
    ProjectStatus,
    Organization,
    Agent,
    Task,
    AgentRun,
    AgentTaskLease,
    AgentAuditEvent,
    ProjectRepoBinding,
)
from core.auth import unified_auth_required, get_current_user
from ..base import ApiResponse
from ..agent_common import now_utc

from . import projects_bp

# AgentRun.state 的活跃/终态集合（触发引擎侧，小写字符串存储）
_RUN_ACTIVE_STATES = ("queued", "leased", "running")
_RUN_FAILED_STATES = ("failed", "expired")
_RUN_WINDOW_DAYS = 90
_RUN_SCAN_CAP = 2000


def _run_is_active(run) -> bool:
    state = (run.state or "").lower()
    if state:
        return state in _RUN_ACTIVE_STATES
    # 协作编排侧 status（按枚举名存储）
    status = getattr(run, "status", None)
    status_name = status.name.lower() if status else ""
    return status_name in _RUN_ACTIVE_STATES


def _run_state_label(run) -> str:
    state = (run.state or "").lower()
    if state:
        return state
    status = getattr(run, "status", None)
    return status.name.lower() if status else "unknown"


@projects_bp.route('/<int:project_id>/overview', methods=['GET'])
@unified_auth_required
def get_project_overview(project_id: int):
    """项目跨域聚合概览（组织 / repo / Agent 运行画像 / 租约 / 审计事件）。"""
    try:
        current_user = get_current_user()
        project = db.session.get(Project, project_id)
        if not project or project.status == ProjectStatus.DELETED:
            return ApiResponse.error(
                "Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}
            ).to_response()
        if not current_user.can_access_project(project):
            return ApiResponse.error(
                "Access denied", 403, error_details={"code": "PERMISSION_DENIED"}
            ).to_response()

        # ── 组织归属 ──
        organization = None
        if project.organization_id:
            org = db.session.get(Organization, project.organization_id)
            if org:
                organization = {"id": org.id, "name": org.name}

        # ── repo 绑定（复用绑定模型的脱敏 to_dict）──
        binding = ProjectRepoBinding.query.filter_by(project_id=project_id).first()
        repo = binding.to_dict() if binding else None

        # ── 本项目的任务 ID 集合（运行/租约都挂在任务上）──
        project_task_ids = [
            row[0]
            for row in db.session.query(Task.id).filter(Task.project_id == project_id).all()
        ]

        # ── 项目可见 Agent（工作区内，allowed_project_ids 语义与
        #    agent_runtime_pull._resolve_accessible_project_ids 一致）──
        agents_payload = []
        if project.organization_id:
            workspace_agents = (
                Agent.query.filter(Agent.workspace_id == project.organization_id).all()
            )
            visible_agents = [
                agent
                for agent in workspace_agents
                if not agent.allowed_project_ids
                or project_id in (agent.allowed_project_ids or [])
            ]

            # 本项目任务上的运行（近 _RUN_WINDOW_DAYS，聚合在 Python 侧完成，
            # 量级为单项目任务运行数，设上限兜底）
            since = datetime.utcnow() - timedelta(days=_RUN_WINDOW_DAYS)
            runs = (
                AgentRun.query.filter(
                    AgentRun.task_id.in_(project_task_ids or [0]),
                    AgentRun.created_at >= since,
                )
                .order_by(AgentRun.created_at.desc())
                .limit(_RUN_SCAN_CAP)
                .all()
            )

            now = now_utc()
            active_lease_agent_ids = set(
                row[0]
                for row in db.session.query(AgentTaskLease.agent_id)
                .filter(
                    AgentTaskLease.task_id.in_(project_task_ids or [0]),
                    AgentTaskLease.active.is_(True),
                    AgentTaskLease.expires_at > now,
                )
                .all()
            )

            stats_by_agent: dict = {}
            last_run_by_agent: dict = {}
            active_runs_by_agent: dict = {}
            for run in runs:
                bucket = stats_by_agent.setdefault(
                    run.agent_id, {"total": 0, "succeeded": 0, "failed": 0}
                )
                bucket["total"] += 1
                state = _run_state_label(run)
                if state == "succeeded":
                    bucket["succeeded"] += 1
                elif state in _RUN_FAILED_STATES:
                    bucket["failed"] += 1
                if run.id not in last_run_by_agent:
                    last_run_by_agent[run.agent_id] = run
                if _run_is_active(run):
                    active_runs_by_agent[run.agent_id] = (
                        active_runs_by_agent.get(run.agent_id, 0) + 1
                    )

            for agent in visible_agents:
                bucket = stats_by_agent.get(agent.id, {"total": 0, "succeeded": 0, "failed": 0})
                last_run = last_run_by_agent.get(agent.id)
                agents_payload.append(
                    {
                        "id": agent.id,
                        "name": agent.name,
                        "display_name": agent.display_name,
                        "status": agent.status.value if agent.status else None,
                        "avatar_url": agent.avatar_url,
                        "execution_mode": agent.execution_mode,
                        "sandbox_profile": agent.sandbox_profile,
                        "explicitly_allowed": bool(agent.allowed_project_ids),
                        "has_active_lease": agent.id in active_lease_agent_ids,
                        "runs_total": bucket["total"],
                        "runs_succeeded": bucket["succeeded"],
                        "runs_failed": bucket["failed"],
                        "active_runs": active_runs_by_agent.get(agent.id, 0),
                        "last_run_state": _run_state_label(last_run) if last_run else None,
                        "last_run_at": (
                            (last_run.started_at or last_run.created_at).isoformat()
                            if last_run and (last_run.started_at or last_run.created_at)
                            else None
                        ),
                    }
                )
            # 显式授权的排前面，其次按运行数
            agents_payload.sort(
                key=lambda a: (not a["explicitly_allowed"], -a["runs_total"], a["id"])
            )

        # ── 运行汇总 ──
        active_runs = sum(a["active_runs"] for a in agents_payload)
        runs_summary = {
            "active": active_runs,
            "running_tasks": len(
                {
                    row[0]
                    for row in db.session.query(AgentTaskLease.task_id)
                    .filter(
                        AgentTaskLease.task_id.in_(project_task_ids or [0]),
                        AgentTaskLease.active.is_(True),
                        AgentTaskLease.expires_at > now_utc(),
                    )
                    .all()
                }
            ),
            "succeeded_window": sum(a["runs_succeeded"] for a in agents_payload),
            "failed_window": sum(a["runs_failed"] for a in agents_payload),
            "window_days": _RUN_WINDOW_DAYS,
        }

        # ── 本项目审计事件 ──
        # 现状：写入方基本不填 project_id，但 task.leased/committed 等运行时
        # 事件带 task_id。因此取最新 500 条（occurred_at 有索引）后按
        # project_id 或项目任务 ID 匹配，兼顾成本与召回；写入方补全
        # project_id 后可退回纯索引过滤。
        task_id_set = set(project_task_ids or [])
        candidate_events = (
            AgentAuditEvent.query.order_by(AgentAuditEvent.occurred_at.desc())
            .limit(500)
            .all()
        )
        matched_events = [
            row
            for row in candidate_events
            if row.project_id == project_id
            or (row.task_id is not None and row.task_id in task_id_set)
        ][:20]
        recent_events = [
            {
                "id": row.id,
                "event_type": row.event_type,
                "level": row.level,
                "actor_type": row.actor_type,
                "actor_agent_id": row.actor_agent_id,
                "task_id": row.task_id,
                "duration_ms": row.duration_ms,
                "error_code": row.error_code,
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            }
            for row in matched_events
        ]

        return ApiResponse.success(
            data={
                "project_id": project_id,
                "organization": organization,
                "repo": repo,
                "agents": agents_payload,
                "runs_summary": runs_summary,
                "recent_events": recent_events,
            },
            message="Project overview retrieved",
        ).to_response()
    except Exception as e:  # noqa: BLE001 - 与包内既有路由一致的兜底
        db.session.rollback()
        return ApiResponse.error(
            f"Failed to load project overview: {e}", 500,
            error_details={"code": "OVERVIEW_FAILED"},
        ).to_response()
