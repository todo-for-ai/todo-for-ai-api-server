"""
团队任务编排模型

支持多 Agent 协作处理任务的编排机制
"""

import enum
from sqlalchemy import Column, String, Text, Enum, Integer, BigInteger, ForeignKey, JSON, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseModel


class OrchestrationStrategy(enum.Enum):
    """编排策略"""

    SEQUENTIAL = 'sequential'      # 顺序执行
    PARALLEL = 'parallel'          # 并行执行
    MAP_REDUCE = 'map_reduce'      # MapReduce 模式
    DEBATE = 'debate'              # 辩论模式
    VOTING = 'voting'              # 投票模式


class OrchestrationStatus(enum.Enum):
    """编排状态"""

    PENDING = 'pending'            # 等待启动
    RUNNING = 'running'            # 执行中
    PAUSED = 'paused'              # 已暂停
    COMPLETED = 'completed'        # 已完成
    FAILED = 'failed'              # 失败
    CANCELLED = 'cancelled'        # 已取消


class TeamTaskOrchestration(BaseModel):
    """
    团队任务编排实例

    记录一个任务的团队编排状态和配置
    """

    __tablename__ = 'team_task_orchestrations'

    team_id = Column(Integer, ForeignKey('agent_teams.id'), nullable=False, index=True,
                     comment='团队ID')
    task_id = Column(BigInteger, ForeignKey('tasks.id'), nullable=False, index=True,
                     comment='任务ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='工作区ID')
    created_by_user_id = Column(Integer, ForeignKey('users.id'), nullable=False,
                                comment='创建者用户ID')

    # 编排配置
    strategy = Column(Enum(OrchestrationStrategy), nullable=False,
                      comment='编排策略')
    participating_agent_ids = Column(JSON, comment='参与的 Agent ID 列表')
    role_assignments = Column(JSON, comment='角色 → Agent ID 映射 (如 {"developer":3,"reviewer":5,"tester":7})')

    # 状态
    status = Column(Enum(OrchestrationStatus), default=OrchestrationStatus.PENDING,
                    nullable=False, comment='状态')

    # 当前执行阶段
    current_stage = Column(Integer, default=0, comment='当前阶段索引')
    total_stages = Column(Integer, default=0, comment='总阶段数')

    # 结果聚合
    output_aggregator_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=True,
                                        comment='结果聚合 Agent ID')
    result_payload = Column(JSON, comment='结果数据')

    # 配置
    config = Column(JSON, comment='编排配置参数')

    # 时间记录
    started_at = Column(DateTime, nullable=True, comment='开始时间')
    completed_at = Column(DateTime, nullable=True, comment='完成时间')

    # 关系
    team = relationship('AgentTeam', foreign_keys=[team_id])
    task = relationship('Task', foreign_keys=[task_id])
    workspace = relationship('Organization', foreign_keys=[workspace_id])
    output_aggregator = relationship('Agent', foreign_keys=[output_aggregator_agent_id])
    subtasks = relationship('TeamSubtask', back_populates='orchestration',
                            cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self, include_subtasks=False):
        data = super().to_dict()
        data['strategy'] = self.strategy.value if self.strategy else None
        data['status'] = self.status.value if self.status else None
        data['participating_agent_ids'] = self.participating_agent_ids or []
        data['result_payload'] = self.result_payload or {}
        data['config'] = self.config or {}

        # 时间字段格式化
        for field in ['started_at', 'completed_at']:
            value = getattr(self, field)
            if value:
                data[field] = value.isoformat()

        if include_subtasks:
            data['subtasks'] = [s.to_dict() for s in self.subtasks.order_by(TeamSubtask.stage_index, TeamSubtask.order_index).all()]

        return data


class SubtaskStatus(enum.Enum):
    """子任务状态"""

    PENDING = 'pending'            # 等待执行
    ASSIGNED = 'assigned'          # 已分配
    RUNNING = 'running'            # 执行中
    COMPLETED = 'completed'        # 已完成
    FAILED = 'failed'              # 失败
    SKIPPED = 'skipped'            # 已跳过
    BLOCKED = 'blocked'            # 被阻塞（等待前置任务）


class TeamSubtask(BaseModel):
    """
    团队子任务

    任务编排中的原子单元
    """

    __tablename__ = 'team_subtasks'

    orchestration_id = Column(Integer, ForeignKey('team_task_orchestrations.id'), nullable=False, index=True,
                              comment='编排实例ID')
    assigned_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, index=True,
                               comment='分配的 Agent ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True,
                          comment='工作区ID')

    # 子任务信息
    title = Column(String(255), nullable=False, comment='子任务标题')
    description = Column(Text, comment='子任务描述')

    # 位置和顺序
    stage_index = Column(Integer, default=0, comment='阶段索引')
    order_index = Column(Integer, default=0, comment='同阶段内排序')

    # 依赖关系
    depends_on_subtask_ids = Column(JSON, comment='依赖的子任务ID列表')

    # 状态
    status = Column(Enum(SubtaskStatus), default=SubtaskStatus.PENDING,
                    nullable=False, comment='状态')

    # 输入输出
    input_payload = Column(JSON, comment='输入数据')
    output_payload = Column(JSON, comment='输出结果')

    # 执行记录
    started_at = Column(DateTime, nullable=True, comment='开始时间')
    completed_at = Column(DateTime, nullable=True, comment='完成时间')
    attempt_count = Column(Integer, default=0, comment='尝试次数')
    last_error = Column(Text, comment='最后错误信息')

    # 关系
    orchestration = relationship('TeamTaskOrchestration', foreign_keys=[orchestration_id], back_populates='subtasks')
    assigned_agent = relationship('Agent', foreign_keys=[assigned_agent_id])

    def to_dict(self):
        data = super().to_dict()
        data['status'] = self.status.value if self.status else None
        data['depends_on_subtask_ids'] = self.depends_on_subtask_ids or []
        data['input_payload'] = self.input_payload or {}
        data['output_payload'] = self.output_payload or {}

        # 时间字段格式化
        for field in ['started_at', 'completed_at']:
            value = getattr(self, field)
            if value:
                data[field] = value.isoformat()

        # 包含 Agent 基本信息
        if self.assigned_agent:
            data['assigned_agent'] = {
                'id': self.assigned_agent.id,
                'name': self.assigned_agent.name,
                'display_name': self.assigned_agent.display_name,
                'avatar_url': self.assigned_agent.avatar_url,
            }

        return data
