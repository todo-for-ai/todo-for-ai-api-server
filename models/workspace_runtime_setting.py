"""
工作区运行时配额与回收策略模型（云端 Agent 执行 Phase 2）

每个工作区一条记录：max_pods（同时在岗 Agent Pod 上限）、
idle_timeout_minutes（Pod 空闲多久后回收）。缺省行为由
AgentRuntimeController 的类属性与 Config 兜底。
"""

from sqlalchemy import Column, Integer

from .base import BaseModel


class WorkspaceRuntimeSetting(BaseModel):
    __tablename__ = 'workspace_runtime_settings'

    workspace_id = Column(Integer, nullable=False, unique=True, index=True,
                          comment='工作区ID')
    max_pods = Column(Integer, nullable=True,
                      comment='同时在岗 Agent Pod 上限；NULL=用系统默认')
    idle_timeout_minutes = Column(Integer, nullable=True,
                                  comment='Pod 空闲回收阈值（分钟）；NULL=用系统默认')
    max_concurrent_agents = Column(Integer, nullable=True,
                                   comment='同时干活的 Agent 数上限（按活跃租约去重计数）；'
                                           'NULL=用系统默认，0=不限')
    created_by = Column(Integer, nullable=True, comment='创建人用户ID')

    def to_dict(self):
        data = super().to_dict()
        data['max_pods'] = self.max_pods
        data['idle_timeout_minutes'] = self.idle_timeout_minutes
        data['max_concurrent_agents'] = self.max_concurrent_agents
        return data
