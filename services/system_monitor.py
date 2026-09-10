"""系统监控服务（仅管理员消费）：部署初始化状态 + 服务器指标 + Agent 全局视图。

三个纯函数，由 api/system_monitor.py 薄壳暴露：
- get_setup_state()        首次安装感知：管理员/Agent 是否就位（前端据此决定
                           是否展示「部署引导」入口——装完即藏）；
- collect_server_metrics() 服务器传统指标：CPU/内存/负载/磁盘/进程 RSS。
                           psutil 可用则优先；缺失时降级为 stdlib（loadavg、
                           /proc/meminfo、shutil.disk_usage），拿不到的字段
                           置 None 并在 available_psutil 标注；
- collect_agent_metrics()  Agent 维度全局视图：总量/活跃/执行模式分布、
                           remote 反连在线、托管运行时阶段分布、活跃租约、
                           近 24h 尝试吞吐、最近活跃 Agent 列表。
"""

import os
import shutil
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List


def get_setup_state() -> Dict[str, Any]:
    """部署初始化状态（廉价 DB 计数，供菜单门控高频调用）。"""
    from models.agent import Agent
    from models.user import User, UserRole

    has_admin = User.query.filter(User.role == UserRole.ADMIN).count() > 0
    has_agent = Agent.query.count() > 0
    ws_connected = _ws_connected_count()
    return {
        'has_admin': has_admin,
        'has_agent': has_agent,
        'has_connected_agent': ws_connected > 0,
        # 完成定义：有管理员且接入了至少一个 Agent（WS 在线是瞬时态，不参与判定）
        'complete': has_admin and has_agent,
    }


def collect_server_metrics() -> Dict[str, Any]:
    """服务器指标：psutil 优先，stdlib 兜底；拿不到的项为 None。"""
    metrics: Dict[str, Any] = {
        'available_psutil': False,
        'collected_at': datetime.utcnow().isoformat(),
        'cpu': {'count': os.cpu_count(), 'percent': None},
        'load': {'one': None, 'five': None, 'fifteen': None},
        'memory': {'total': None, 'used': None, 'percent': None},
        'disk': _disk_usage(os.getcwd()),
        'process': {'rss_bytes': _process_rss_bytes()},
    }

    loadavg = _load_avg()
    if loadavg:
        one, five, fifteen = loadavg
        metrics['load'] = {'one': one, 'five': five, 'fifteen': fifteen}

    try:
        import psutil

        metrics['available_psutil'] = True
        metrics['cpu']['percent'] = psutil.cpu_percent(interval=0.15)
        memory = psutil.virtual_memory()
        metrics['memory'] = {
            'total': memory.total,
            'used': memory.used,
            'percent': memory.percent,
        }
        process = psutil.Process()
        metrics['process'].update({
            'cpu_percent': process.cpu_percent(interval=None),
            'rss_bytes': process.memory_info().rss,
            'create_time': datetime.utcfromtimestamp(
                process.create_time()).isoformat(),
        })
    except ImportError:
        # stdlib 兜底：/proc/meminfo（Linux）
        mem = _proc_meminfo()
        if mem:
            metrics['memory'] = mem
    except Exception:  # noqa: BLE001 — psutil 异常不阻断其它字段
        pass
    return metrics


def collect_agent_metrics() -> Dict[str, Any]:
    """Agent 维度全局视图（执行环境 × 任务活动）。"""
    from models.agent import Agent
    from models.agent_task_attempt import AgentTaskAttempt, AgentTaskAttemptState
    from models.agent_task_lease import AgentTaskLease
    from services.runtime_env import (
        get_runtime_provider,
        get_runtime_provider_for_agent,
    )

    now = datetime.utcnow()
    total = Agent.query.count()
    active = Agent.query.filter_by(status='ACTIVE').count()
    managed_agents = Agent.query.filter_by(execution_mode='managed_runner').count()

    # 执行环境平面：remote（反连）与部署级托管后端
    remote_online = remote_pending = managed_running = 0
    try:
        for runtime in get_runtime_provider('remote').list_runtimes():
            if runtime.get('phase') == 'Running':
                remote_online += 1
            elif runtime.get('phase') == 'Pending':
                remote_pending += 1
        for runtime in get_runtime_provider().list_runtimes():
            if runtime.get('phase') in ('Running', 'Pending'):
                managed_running += 1
    except Exception:  # noqa: BLE001 — 无集群凭据等场景下监控不崩
        pass

    active_leases = AgentTaskLease.query.filter_by(active=True).filter(
        AgentTaskLease.expires_at > now).count()

    day_ago = now - timedelta(hours=24)
    throughput: Dict[str, int] = {}
    for state in AgentTaskAttemptState:
        count = AgentTaskAttempt.query.filter(
            AgentTaskAttempt.state == state,
            AgentTaskAttempt.started_at >= day_ago,
        ).count()
        throughput[state.name.lower()] = count

    recent: List[Dict[str, Any]] = []
    # MySQL DESC 排序时 NULL 天然在末尾，无需 nullslast（MySQL 不支持该语法）
    for agent in Agent.query.order_by(Agent.last_seen_at.desc()).limit(20).all():
        runtime = get_runtime_provider_for_agent(agent).get_runtime_status(agent.id)
        recent.append({
            'id': agent.id,
            'name': agent.name,
            'status': agent.status.value if agent.status else None,
            'execution_mode': agent.execution_mode,
            'engine': runtime.get('runtime_type') if runtime else None,
            'runtime_phase': runtime.get('phase') if runtime else None,
            'last_seen_at': agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        })

    return {
        'collected_at': now.isoformat(),
        'agents': {
            'total': total,
            'active': active,
            'managed_runner': managed_agents,
            'reverse_connect': total - managed_agents,
        },
        'runtimes': {
            'remote_online': remote_online,
            'remote_pending': remote_pending,
            'managed_occupying': managed_running,
        },
        'tasks': {
            'active_leases': active_leases,
            'attempts_24h': throughput,
        },
        'recent_agents': recent,
    }


# ── 内部工具 ──────────────────────────────────────────────

def _ws_connected_count() -> int:
    try:
        from api.agent_runtime_websocket import _CONNECTED_AGENT_IDS
        return len(_CONNECTED_AGENT_IDS)
    except Exception:  # noqa: BLE001 — 非 Web 进程无注册表
        return 0


def _load_avg():
    if not hasattr(os, 'getloadavg'):
        return None
    try:
        return os.getloadavg()
    except (OSError, ValueError):
        return None


def _disk_usage(path: str):
    try:
        usage = shutil.disk_usage(path)
        return {
            'total': usage.total,
            'used': usage.used,
            'free': usage.free,
            'percent': round(usage.used / usage.total * 100, 1) if usage.total else None,
        }
    except Exception:  # noqa: BLE001
        return {'total': None, 'used': None, 'free': None, 'percent': None}


def _process_rss_bytes():
    try:
        import resource
        # macOS 单位 byte，Linux 单位 KB
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return maxrss * 1024 if sys_platform_is_linux() else maxrss
    except Exception:  # noqa: BLE001
        return None


def sys_platform_is_linux() -> bool:
    return os.uname().sysname.lower() == 'linux' if hasattr(os, 'uname') else False


def _proc_meminfo():
    try:
        with open('/proc/meminfo', encoding='ascii') as fh:
            info = {}
            for line in fh:
                key, _, rest = line.partition(':')
                parts = rest.split()
                if key in ('MemTotal', 'MemAvailable') and parts:
                    info[key] = int(parts[0]) * 1024  # kB → B
        if 'MemTotal' in info and 'MemAvailable' in info and info['MemTotal']:
            used = info['MemTotal'] - info['MemAvailable']
            return {
                'total': info['MemTotal'], 'used': used,
                'percent': round(used / info['MemTotal'] * 100, 1),
            }
    except Exception:  # noqa: BLE001 — 非 Linux 或读取失败
        pass
    return None
