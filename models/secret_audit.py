"""
Agent Secret 审计日志模型

用于记录 Secret 的所有操作，支持合规审计
"""

import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship
from .base import BaseModel


class SecretAuditAction(enum.Enum):
    """Secret 审计操作类型"""
    CREATED = 'created'
    REVEALED = 'revealed'
    ROTATED = 'rotated'
    REVOKED = 'revoked'
    SHARED = 'shared'
    SHARE_REVOKED = 'share_revoked'
    USED = 'used'
    ACCESS_DENIED = 'access_denied'


class SecretAuditLog(BaseModel):
    """Secret 审计日志"""

    __tablename__ = 'secret_audit_logs'

    # 索引优化查询
    __table_args__ = (
        Index('idx_secret_audit_secret_id', 'secret_id'),
        Index('idx_secret_audit_action', 'action'),
        Index('idx_secret_audit_timestamp', 'timestamp'),
        Index('idx_secret_audit_actor', 'actor_type', 'actor_id'),
    )

    secret_id = Column(Integer, ForeignKey('agent_secrets.id'), nullable=False, index=True, comment='Secret ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')

    # 操作信息
    action = Column(String(32), nullable=False, comment='操作类型')
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, comment='操作时间')

    # 执行者信息
    actor_type = Column(String(32), nullable=False, comment='执行者类型: user, agent, system')
    actor_id = Column(Integer, nullable=False, comment='执行者ID')
    actor_name = Column(String(128), comment='执行者名称')

    # 目标信息（用于共享场景）
    target_type = Column(String(32), comment='目标类型: agent, user')
    target_id = Column(Integer, comment='目标ID')
    target_name = Column(String(128), comment='目标名称')

    # 请求上下文
    request_ip = Column(String(45), comment='请求IP地址')
    request_user_agent = Column(Text, comment='User Agent')
    request_id = Column(String(64), comment='关联请求ID')

    # 变更详情
    details = Column(Text, comment='操作详情JSON')
    success = Column(String(1), nullable=False, default='Y', comment='是否成功: Y/N')
    failure_reason = Column(Text, comment='失败原因')

    # 关系
    secret = relationship('AgentSecret', foreign_keys=[secret_id])

    def to_dict(self):
        data = super().to_dict()
        data['action'] = self.action
        data['success'] = self.success == 'Y'
        return data


class SecretApprovalRequest(BaseModel):
    """Secret 共享审批请求"""

    __tablename__ = 'secret_approval_requests'

    secret_id = Column(Integer, ForeignKey('agent_secrets.id'), nullable=False, index=True, comment='Secret ID')
    workspace_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True, comment='工作区ID')

    # 申请信息
    requester_type = Column(String(32), nullable=False, comment='申请者类型: user, agent')
    requester_id = Column(Integer, nullable=False, comment='申请者ID')
    requester_name = Column(String(128), comment='申请者名称')
    request_reason = Column(Text, comment='申请原因')

    # 目标信息
    target_agent_id = Column(Integer, ForeignKey('agents.id'), nullable=False, comment='目标Agent ID')
    access_mode = Column(String(32), default='read', comment='访问模式: read, admin')

    # 状态
    status = Column(String(32), nullable=False, default='pending', comment='状态: pending, approved, rejected')

    # 审批信息
    approver_type = Column(String(32), comment='审批者类型')
    approver_id = Column(Integer, comment='审批者ID')
    approver_name = Column(String(128), comment='审批者名称')
    approved_at = Column(DateTime, comment='审批时间')
    approval_comment = Column(Text, comment='审批意见')

    # 过期时间
    expires_at = Column(DateTime, comment='审批结果过期时间')

    # 关系
    secret = relationship('AgentSecret', foreign_keys=[secret_id])
    target_agent = relationship('Agent', foreign_keys=[target_agent_id])

    def to_dict(self):
        data = super().to_dict()
        data['status'] = self.status
        return data
