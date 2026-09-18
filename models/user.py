"""
用户模型
"""

from datetime import datetime
from enum import Enum
from .base import Base
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON, Enum as SqlEnum

class UserRole(Enum):
    """用户角色枚举 - 匹配数据库实际值"""
    ADMIN = 'ADMIN'
    USER = 'USER'
    VIEWER = 'VIEWER'
    GUEST = 'GUEST'

class UserStatus(Enum):
    """用户状态枚举 - 匹配数据库实际值"""
    ACTIVE = 'ACTIVE'
    INACTIVE = 'INACTIVE'
    SUSPENDED = 'SUSPENDED'

class User(Base):
    """用户模型 - 匹配数据库实际结构"""
    __tablename__ = 'users'

    # OAuth相关
    github_id = Column(String(255), unique=True, comment='GitHub ID')
    google_id = Column(String(255), unique=True, comment='Google ID')

    # 基本信息
    email = Column(String(255), unique=True, nullable=False, comment='邮箱')
    email_verified = Column(Boolean, default=False, comment='邮箱是否验证')
    auth0_user_id = Column(String(255), unique=True, comment='Auth0用户ID')
    username = Column(String(100), comment='用户名')
    name = Column(String(200), comment='显示名称')
    nickname = Column(String(100), comment='昵称')
    full_name = Column(String(200), comment='全名')
    avatar_url = Column(String(500), comment='头像URL')
    bio = Column(Text, comment='个人简介')

    # OAuth提供商信息
    provider = Column(String(50), comment='OAuth提供商')
    provider_user_id = Column(String(255), comment='提供商用户ID')

    # 角色和状态 - 使用数据库实际的枚举值
    role = Column(SqlEnum(UserRole), default=UserRole.USER, nullable=False, comment='用户角色')
    status = Column(SqlEnum(UserStatus), default=UserStatus.ACTIVE, nullable=False, comment='用户状态')

    # 登录时间
    last_login = Column(DateTime, comment='最后登录时间')
    last_login_at = Column(DateTime, comment='最后登录时间(别名)')
    last_active_at = Column(DateTime, comment='最后活跃时间')

    # 偏好设置
    preferences = Column(JSON, comment='用户偏好设置JSON')
    timezone = Column(String(50), comment='时区')
    locale = Column(String(10), comment='语言设置')

    def is_active(self):
        """检查用户是否活跃"""
        return self.status == UserStatus.ACTIVE

    def __repr__(self):
        return f'<User {self.username or self.email}>'
