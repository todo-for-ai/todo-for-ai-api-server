"""
工作区运行时配额与空闲回收（云端 Agent 执行 Phase 2）

- get_workspace_runtime_setting(controller, workspace_id)：DB 记录优先，
  回退 Config 与控制器类默认；
- recycle_idle_pods(controller, limit=100)：回收空闲 Agent Pod（幂等，
  供看门狗周期调用）。

全部函数显式接收 controller，便于测试注入 fake。
"""

from datetime import datetime, timedelta

from core.config import Config
from models import AgentTaskAttempt, WorkspaceRuntimeSetting
from utils.logger import logger


def get_workspace_runtime_setting(controller, workspace_id: int) -> dict:
    """读取工作区配额与回收策略：DB 记录 > Config > 控制器类默认。"""
    default_cap = int(getattr(Config, 'K8S_MAX_PODS_PER_WORKSPACE', 0)
                      or controller.MAX_PODS_PER_WORKSPACE)
    default_idle = int(getattr(Config, 'K8S_POD_IDLE_TIMEOUT_MINUTES', 0)
                       or controller.POD_IDLE_TIMEOUT_MINUTES)
    cap, idle = default_cap, default_idle
    row = WorkspaceRuntimeSetting.query.filter_by(workspace_id=workspace_id).first()
    if row:
        if row.max_pods is not None:
            cap = int(row.max_pods)
        if row.idle_timeout_minutes is not None:
            idle = int(row.idle_timeout_minutes)
    return {'max_pods': cap, 'idle_timeout_minutes': idle}


def set_workspace_runtime_setting(workspace_id: int, max_pods=None,
                                  idle_timeout_minutes=None) -> WorkspaceRuntimeSetting:
    """创建/更新工作区运行时设置（None 字段保持不变）。"""
    row = WorkspaceRuntimeSetting.query.filter_by(workspace_id=workspace_id).first()
    if not row:
        row = WorkspaceRuntimeSetting(workspace_id=workspace_id)
    if max_pods is not None:
        row.max_pods = max(0, int(max_pods))
    if idle_timeout_minutes is not None:
        row.idle_timeout_minutes = max(0, int(idle_timeout_minutes))
    from models import db
    db.session.add(row)
    db.session.commit()
    return row


def recycle_idle_pods(controller, limit: int = 100) -> dict:
    """回收空闲 Agent Pod（幂等，供看门狗周期调用）。

    活动信号 = 该 Agent 最近一次任务 attempt（ended_at 或 started_at）；
    从未有任务的 Pod 以 Pod Ready 时间计。有 ACTIVE attempt 的一律不回收
    （正在干活）。阈值来自 get_workspace_runtime_setting；0 = 不回收。
    """
    now = datetime.utcnow()
    result = {'checked': 0, 'recycled': 0, 'skipped_active': 0, 'skipped_recent': 0}

    try:
        pods = controller._list_all_agent_pods()
    except Exception as e:  # noqa: BLE001  无集群配置时静默跳过
        logger.warning("runtime.recycle_no_cluster error=%s", e)
        return result

    setting_cache = {}
    for pod in pods[:limit]:
        result['checked'] += 1
        labels = pod.metadata.labels if pod.metadata else {}
        try:
            agent_id = int((labels or {}).get('agent-id', 0) or 0)
            workspace_id = int((labels or {}).get('workspace-id', 0) or 0)
        except (TypeError, ValueError):
            continue
        if not agent_id:
            continue
        if pod.status and pod.status.phase not in ('Running', 'Pending'):
            continue

        if workspace_id not in setting_cache:
            setting_cache[workspace_id] = get_workspace_runtime_setting(
                controller, workspace_id
            )
        idle_minutes = setting_cache[workspace_id]['idle_timeout_minutes']
        if idle_minutes <= 0:
            continue

        active = AgentTaskAttempt.query.filter(
            AgentTaskAttempt.agent_id == agent_id,
            AgentTaskAttempt.state == 'ACTIVE',
            # 只认阈值窗口内开始的 attempt：被强删 Pod 遗留的陈旧 ACTIVE 行
            # 不应永久阻塞回收（租约级活动判定留待 Phase 3 观测面）
            AgentTaskAttempt.started_at >= now - timedelta(minutes=idle_minutes),
        ).first()
        if active:
            result['skipped_active'] += 1
            continue

        last = _last_activity_at(agent_id) or _pod_ready_at(pod) or now
        idle = (now - last).total_seconds() / 60.0
        if idle < idle_minutes:
            result['skipped_recent'] += 1
            continue

        if controller.terminate_agent_pod(agent_id):
            result['recycled'] += 1
            logger.info(
                "runtime.idle_pod_recycled agent_id=%s workspace_id=%s idle_minutes=%s",
                agent_id, workspace_id, round(idle, 1),
            )
    return result


def _last_activity_at(agent_id: int):
    row = (
        AgentTaskAttempt.query.filter_by(agent_id=agent_id)
        .order_by(AgentTaskAttempt.id.desc())
        .first()
    )
    if not row:
        return None
    return row.ended_at or row.started_at


def _pod_ready_at(pod):
    try:
        for condition in (pod.status.conditions or []):
            if condition.type == 'Ready' and condition.last_transition_time:
                return condition.last_transition_time.replace(tzinfo=None)
    except (AttributeError, TypeError):
        pass
    return None
