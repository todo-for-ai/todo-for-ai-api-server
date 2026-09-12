"""租约时长策略（统一解析，替代散落在派发路径上的 60s 硬编码）。

长跑任务（GoalLoop 多日循环、大改造单任务）需要远超 60s 的租约；
租约 TTL 过短会让续约节奏稍一抖动就 LEASE_EXPIRED，工作成果作废。
解析优先级：Agent 激活运行时配置 > 环境变量 LEASE_DURATION_SECONDS > 默认 120s，
结果钳制在 [60, 3600] 秒。
"""

import os

DEFAULT_LEASE_TTL_SECONDS = 120
MIN_LEASE_TTL_SECONDS = 60
MAX_LEASE_TTL_SECONDS = 3600


def _env_default_ttl() -> int:
    raw = os.environ.get('LEASE_DURATION_SECONDS')
    if not raw:
        return DEFAULT_LEASE_TTL_SECONDS
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LEASE_TTL_SECONDS


def effective_lease_ttl(agent_id=None, workspace_id=None) -> int:
    """解析某 agent/工作区的租约时长（秒）。两个参数都可缺省（取环境默认）。"""
    del workspace_id  # 预留工作区级覆盖；当前配置按 agent 版本化
    ttl = None
    if agent_id is not None:
        try:
            from models import db
            from models.agent_runtime_monitor import AgentRuntimeConfig

            cfg = (
                AgentRuntimeConfig.query
                .filter_by(agent_id=int(agent_id), is_active=True)
                .order_by(AgentRuntimeConfig.version.desc())
                .first()
            )
            if cfg and cfg.lease_duration_seconds:
                ttl = int(cfg.lease_duration_seconds)
        except Exception:  # noqa: BLE001 - 配置读取失败回落环境默认，绝不阻断派发
            ttl = None
    if ttl is None:
        ttl = _env_default_ttl()
    return max(MIN_LEASE_TTL_SECONDS, min(MAX_LEASE_TTL_SECONDS, ttl))
