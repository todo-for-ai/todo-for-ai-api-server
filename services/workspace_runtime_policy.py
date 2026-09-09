"""
工作区运行时配额与空闲回收（云端 Agent 执行 Phase 2）

- get_workspace_runtime_setting(controller, workspace_id)：DB 记录优先，
  回退 Config 与控制器类默认；
- recycle_idle_pods(controller, limit=100)：回收空闲 Agent Pod（幂等，
  供看门狗周期调用）。
- check_dispatch_capacity(workspace_id, agent_id, controller=None)：
  多 Agent 编排的「同时干活」并发门禁（按未过期活跃租约去重计数）。

全部函数显式接收 controller，便于测试注入 fake。
"""

from datetime import datetime, timedelta

from core.config import Config
from models import AgentTaskAttempt, AgentTaskLease, WorkspaceRuntimeSetting
from services.runtime_env.base import OCCUPYING_PHASES
from utils.logger import logger

# 工作区「同时干活」Agent 数的系统默认上限（Config 可覆盖；0=不限）。
# 按「持有未过期活跃租约的 distinct agent」计数——允许一个在岗 Agent 领多任务，
# 但想再拉起一个新的 Agent 干活就受此上限约束（云端资源/用户 token 有限）。
DEFAULT_MAX_CONCURRENT_AGENTS = 5


def get_workspace_runtime_setting(controller, workspace_id: int) -> dict:
    """读取工作区配额与回收策略：DB 记录 > Config > 默认值。"""
    default_cap = int(getattr(Config, 'K8S_MAX_PODS_PER_WORKSPACE', 0)
                      or getattr(controller, 'MAX_PODS_PER_WORKSPACE', 0) or 10)
    default_idle = int(getattr(Config, 'K8S_POD_IDLE_TIMEOUT_MINUTES', 0)
                       or getattr(controller, 'POD_IDLE_TIMEOUT_MINUTES', 0) or 30)
    default_agents = int(getattr(Config, 'ORCHESTRATION_MAX_CONCURRENT_AGENTS', 0)
                         or DEFAULT_MAX_CONCURRENT_AGENTS)
    cap, idle, agents = default_cap, default_idle, default_agents
    row = WorkspaceRuntimeSetting.query.filter_by(workspace_id=workspace_id).first()
    if row:
        if row.max_pods is not None:
            cap = int(row.max_pods)
        if row.idle_timeout_minutes is not None:
            idle = int(row.idle_timeout_minutes)
        if row.max_concurrent_agents is not None:
            agents = int(row.max_concurrent_agents)
    return {
        'max_pods': cap,
        'idle_timeout_minutes': idle,
        'max_concurrent_agents': agents,
    }


def set_workspace_runtime_setting(workspace_id: int, max_pods=None,
                                  idle_timeout_minutes=None,
                                  max_concurrent_agents=None) -> WorkspaceRuntimeSetting:
    """创建/更新工作区运行时设置（None 字段保持不变）。"""
    row = WorkspaceRuntimeSetting.query.filter_by(workspace_id=workspace_id).first()
    if not row:
        row = WorkspaceRuntimeSetting(workspace_id=workspace_id)
    if max_pods is not None:
        row.max_pods = max(0, int(max_pods))
    if idle_timeout_minutes is not None:
        row.idle_timeout_minutes = max(0, int(idle_timeout_minutes))
    if max_concurrent_agents is not None:
        row.max_concurrent_agents = max(0, int(max_concurrent_agents))
    from models import db
    db.session.add(row)
    db.session.commit()
    return row


def recycle_idle_pods(provider, limit: int = 100) -> dict:
    """回收空闲运行时（幂等，供看门狗周期调用）。

    活动信号 = 该 Agent 最近一次任务 attempt（ended_at 或 started_at）；
    从未有任务的实例以启动时间计。有 ACTIVE attempt 的一律不回收
    （正在干活）。阈值来自 get_workspace_runtime_setting；0 = 不回收。
    provider 为任意 RuntimeProvider 后端（k8s/docker/compose/baremetal），
    状态按 services/runtime_env/base 的归一化契约读取。
    """
    now = datetime.utcnow()
    result = {'checked': 0, 'recycled': 0, 'skipped_active': 0, 'skipped_recent': 0}

    try:
        runtimes = provider.list_runtimes()
    except Exception as e:  # noqa: BLE001  后端不可达时静默跳过
        logger.warning("runtime.recycle_backend_unreachable error=%s", e)
        return result

    setting_cache = {}
    for entry in runtimes[:limit]:
        result['checked'] += 1
        agent_id = entry.get('agent_id')
        workspace_id = entry.get('workspace_id')
        if not agent_id:
            continue
        if entry.get('phase') not in OCCUPYING_PHASES:
            continue

        if workspace_id not in setting_cache:
            setting_cache[workspace_id] = get_workspace_runtime_setting(
                provider, workspace_id
            )
        idle_minutes = setting_cache[workspace_id]['idle_timeout_minutes']
        if idle_minutes <= 0:
            continue

        active = AgentTaskAttempt.query.filter(
            AgentTaskAttempt.agent_id == agent_id,
            AgentTaskAttempt.state == 'ACTIVE',
            # 只认阈值窗口内开始的 attempt：被强删实例遗留的陈旧 ACTIVE 行
            # 不应永久阻塞回收（租约级活动判定留待观测面）
            AgentTaskAttempt.started_at >= now - timedelta(minutes=idle_minutes),
        ).first()
        if active:
            result['skipped_active'] += 1
            continue

        last = _last_activity_at(agent_id) or _parse_ts(entry.get('started_at')) or now
        idle = (now - last).total_seconds() / 60.0
        if idle < idle_minutes:
            result['skipped_recent'] += 1
            continue

        if provider.terminate(agent_id):
            result['recycled'] += 1
            logger.info(
                "runtime.idle_instance_recycled agent_id=%s workspace_id=%s "
                "idle_minutes=%s backend=%s",
                agent_id, workspace_id, round(idle, 1),
                getattr(provider, 'name', '?'),
            )
    return result


def _parse_ts(value):
    """归一化 started_at（ISO 字符串或 datetime）→ naive datetime；失败返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _last_activity_at(agent_id: int):
    row = (
        AgentTaskAttempt.query.filter_by(agent_id=agent_id)
        .order_by(AgentTaskAttempt.id.desc())
        .first()
    )
    if not row:
        return None
    return row.ended_at or row.started_at


# ────────────────────── 多 Agent 编排并发门禁 ──────────────────────

def agent_active_lease_count(agent_id: int) -> int:
    """某 Agent 当前持有的未过期活跃租约数（正在干活的任务数）。"""
    now = datetime.utcnow()
    return AgentTaskLease.query.filter(
        AgentTaskLease.agent_id == agent_id,
        AgentTaskLease.active.is_(True),
        AgentTaskLease.expires_at > now,
    ).count()


def active_agent_count(workspace_id: int) -> int:
    """工作区内「正在干活」的 distinct Agent 数（未过期活跃租约去重）。"""
    now = datetime.utcnow()
    rows = (
        AgentTaskLease.query.filter(
            AgentTaskLease.workspace_id == workspace_id,
            AgentTaskLease.active.is_(True),
            AgentTaskLease.expires_at > now,
        )
        .with_entities(AgentTaskLease.agent_id)
        .distinct()
        .all()
    )
    return len(rows)


def check_dispatch_capacity(workspace_id: int, agent_id: int,
                            controller=None) -> dict:
    """派发前的编排并发门禁。

    语义（「最多多少个 Agent 同时干活」按 distinct Agent 计，不按任务计）：
    - max_concurrent_agents <= 0 → 不限，放行；
    - 该 Agent 已在干活（持有未过期活跃租约）→ 不新增并发，放行；
    - 否则仅当当前干活 Agent 数 < 上限时放行。

    返回 {'allowed': bool, 'limit': int, 'active_agents': int, 'reason': str|None}。
    """
    limit = int(get_workspace_runtime_setting(controller, workspace_id)
                ['max_concurrent_agents'])
    active = active_agent_count(workspace_id)
    result = {'limit': limit, 'active_agents': active, 'allowed': True, 'reason': None}
    if limit <= 0 or active < limit:
        return result
    if agent_active_lease_count(agent_id) > 0:
        return result
    result.update({'allowed': False, 'reason': 'WORKSPACE_AGENT_CONCURRENCY_LIMIT'})
    return result


