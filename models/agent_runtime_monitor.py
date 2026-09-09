"""
Agent Runtime 监控相关模型

用于存储 Agent 的心跳、指标和运行时配置
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, JSON, BigInteger, Index
from sqlalchemy.orm import relationship
from .base import BaseModel, db


class AgentHeartbeat(BaseModel):
    """Agent 心跳记录"""

    __tablename__ = 'agent_heartbeats'
    # 最新心跳查询热路径：get_latest_by_agent（按 Agent 取最新一条）
    __table_args__ = (
        Index('idx_agent_heartbeats_agent_created', 'agent_id', 'created_at'),
    )

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    session_token_prefix = Column(String(16), comment='会话Token前缀')

    # 心跳数据
    status = Column(String(32), nullable=False, default='running', comment='状态: running, idle, busy, error')
    active_tasks = Column(Integer, default=0, comment='当前活跃任务数')
    uptime_seconds = Column(Integer, comment='运行时长(秒)')
    extra = Column(JSON, comment='额外数据')

    # 运行时信息（从心跳中提取）
    runtime_version = Column(String(32), comment='运行时版本')
    runtime_type = Column(String(32), comment='运行时类型: openclaw, custom')
    ip_address = Column(String(64), comment='IP地址')
    hostname = Column(String(256), comment='主机名')

    # 关系
    agent = relationship('Agent')
    workspace = relationship('Organization')

    @classmethod
    def record_heartbeat(cls, agent_id: int, workspace_id: int, data: dict):
        """记录心跳"""
        heartbeat = cls(
            agent_id=agent_id,
            workspace_id=workspace_id,
            status=data.get('status', 'running'),
            active_tasks=data.get('active_tasks', 0),
            uptime_seconds=data.get('uptime_seconds'),
            extra=data.get('extra'),
            runtime_version=data.get('extra', {}).get('runtime_version') if data.get('extra') else None,
            runtime_type=data.get('extra', {}).get('runtime_type') if data.get('extra') else None,
        )
        db.session.add(heartbeat)
        db.session.commit()
        return heartbeat

    @classmethod
    def get_latest_by_agent(cls, agent_id: int):
        """获取 Agent 最新心跳"""
        return cls.query.filter_by(agent_id=agent_id).order_by(cls.created_at.desc()).first()

    @classmethod
    def get_agents_by_status(cls, workspace_id: int, status: str, since: datetime):
        """获取指定状态的 Agent 列表"""
        from sqlalchemy import func
        subq = db.session.query(
            cls.agent_id,
            func.max(cls.created_at).label('latest_ts')
        ).filter(
            cls.workspace_id == workspace_id,
            cls.created_at >= since
        ).group_by(cls.agent_id).subquery()

        return cls.query.join(
            subq,
            (cls.agent_id == subq.c.agent_id) &
            (cls.created_at == subq.c.latest_ts)
        ).filter(cls.status == status).all()


class AgentMetrics(BaseModel):
    """Agent 指标数据"""

    __tablename__ = 'agent_metrics'

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')
    session_id = Column(Integer, ForeignKey('agent_sessions.id'), comment='会话ID')

    # 系统指标
    cpu_percent = Column(Float, comment='CPU使用率')
    memory_usage_mb = Column(Float, comment='内存使用(MB)')
    memory_percent = Column(Float, comment='内存使用率(%)')
    disk_usage_percent = Column(Float, comment='磁盘使用率(%)')
    network_in_bytes = Column(BigInteger, comment='网络流入字节')
    network_out_bytes = Column(BigInteger, comment='网络流出字节')

    # 任务指标
    tasks_completed = Column(Integer, comment='已完成任务数')
    tasks_failed = Column(Integer, comment='失败任务数')
    tasks_active = Column(Integer, comment='活跃任务数')
    tasks_queued = Column(Integer, comment='排队任务数')

    # 运行时指标
    uptime_seconds = Column(Integer, comment='运行时长(秒)')
    request_latency_ms = Column(Float, comment='请求延迟(ms)')

    # 扩展指标
    extra = Column(JSON, comment='额外指标')

    # 关系
    agent = relationship('Agent')
    workspace = relationship('Organization')
    session = relationship('AgentSession')

    @classmethod
    def record_metrics(cls, agent_id: int, workspace_id: int, data: dict):
        """记录指标"""
        metrics = cls(
            agent_id=agent_id,
            workspace_id=workspace_id,
            cpu_percent=data.get('cpu_percent'),
            memory_usage_mb=data.get('memory_usage_mb'),
            memory_percent=data.get('memory_percent'),
            disk_usage_percent=data.get('disk_usage_percent'),
            network_in_bytes=data.get('network_in_bytes'),
            network_out_bytes=data.get('network_out_bytes'),
            tasks_completed=data.get('tasks_completed'),
            tasks_failed=data.get('tasks_failed'),
            tasks_active=data.get('tasks_active'),
            tasks_queued=data.get('tasks_queued'),
            uptime_seconds=data.get('uptime_seconds'),
            request_latency_ms=data.get('request_latency_ms'),
            extra=data.get('extra'),
        )
        db.session.add(metrics)
        db.session.commit()
        return metrics

    @classmethod
    def get_latest_metrics(cls, agent_id: int):
        """获取最新指标"""
        return cls.query.filter_by(agent_id=agent_id).order_by(cls.created_at.desc()).first()

    @classmethod
    def get_metrics_history(cls, agent_id: int, limit: int = 100):
        """获取指标历史"""
        return cls.query.filter_by(agent_id=agent_id).order_by(cls.created_at.desc()).limit(limit).all()


class AgentRuntimeConfig(BaseModel):
    """Agent 运行时配置（版本化）"""

    __tablename__ = 'agent_runtime_configs'

    agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True, comment='Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')

    # 配置版本
    version = Column(Integer, nullable=False, default=1, comment='配置版本号')
    is_active = Column(db.Boolean, default=True, comment='是否激活')

    # 运行时配置
    max_concurrent_tasks = Column(Integer, default=5, comment='最大并发任务数')
    heartbeat_interval_seconds = Column(Integer, default=30, comment='心跳间隔(秒)')
    metrics_report_interval_seconds = Column(Integer, default=60, comment='指标上报间隔(秒)')
    config_sync_interval_seconds = Column(Integer, default=300, comment='配置同步间隔(秒)')
    task_poll_interval_seconds = Column(Integer, default=5, comment='任务拉取间隔(秒)')

    # 任务执行配置
    task_timeout_seconds = Column(Integer, default=1800, comment='任务超时(秒)')
    task_max_retry = Column(Integer, default=2, comment='任务最大重试')
    lease_duration_seconds = Column(Integer, default=120, comment='租约时长(秒)')
    lease_renewal_interval_seconds = Column(Integer, default=60, comment='租约续约间隔(秒)')

    # 日志配置
    log_level = Column(String(32), default='INFO', comment='日志级别')
    log_max_lines = Column(Integer, default=10000, comment='最大日志行数')

    # 扩展配置
    extra_config = Column(JSON, comment='扩展配置')

    # 关系
    agent = relationship('Agent', backref='runtime_configs')
    workspace = relationship('Organization')

    @classmethod
    def get_active_config(cls, agent_id: int):
        """获取 Agent 激活的配置"""
        return cls.query.filter_by(
            agent_id=agent_id,
            is_active=True
        ).order_by(cls.version.desc()).first()

    @classmethod
    def create_config(cls, agent_id: int, workspace_id: int, data: dict):
        """创建新配置版本"""
        # 停用旧配置
        cls.query.filter_by(agent_id=agent_id, is_active=True).update({'is_active': False})

        # 获取最新版本号
        latest = cls.query.filter_by(agent_id=agent_id).order_by(cls.version.desc()).first()
        new_version = (latest.version + 1) if latest else 1

        config = cls(
            agent_id=agent_id,
            workspace_id=workspace_id,
            version=new_version,
            is_active=True,
            max_concurrent_tasks=data.get('max_concurrent_tasks', 5),
            heartbeat_interval_seconds=data.get('heartbeat_interval_seconds', 30),
            metrics_report_interval_seconds=data.get('metrics_report_interval_seconds', 60),
            config_sync_interval_seconds=data.get('config_sync_interval_seconds', 300),
            task_poll_interval_seconds=data.get('task_poll_interval_seconds', 5),
            task_timeout_seconds=data.get('task_timeout_seconds', 1800),
            task_max_retry=data.get('task_max_retry', 2),
            lease_duration_seconds=data.get('lease_duration_seconds', 120),
            lease_renewal_interval_seconds=data.get('lease_renewal_interval_seconds', 60),
            log_level=data.get('log_level', 'INFO'),
            log_max_lines=data.get('log_max_lines', 10000),
            extra_config=data.get('extra_config'),
        )
        db.session.add(config)
        db.session.commit()
        return config

    def to_dict(self):
        """转换为字典（返回给 Agent）"""
        return {
            'version': self.version,
            'max_concurrent_tasks': self.max_concurrent_tasks,
            'heartbeat_interval_seconds': self.heartbeat_interval_seconds,
            'metrics_report_interval_seconds': self.metrics_report_interval_seconds,
            'config_sync_interval_seconds': self.config_sync_interval_seconds,
            'task_poll_interval_seconds': self.task_poll_interval_seconds,
            'task_timeout_seconds': self.task_timeout_seconds,
            'task_max_retry': self.task_max_retry,
            'lease_duration_seconds': self.lease_duration_seconds,
            'lease_renewal_interval_seconds': self.lease_renewal_interval_seconds,
            'log_level': self.log_level,
            'log_max_lines': self.log_max_lines,
            'extra_config': self.extra_config or {},
        }
