"""
用户模型
"""

from datetime import datetime
from enum import Enum
from .base import Base
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, Enum as SqlEnum

class UserRole(Enum):
    """用户角色枚举"""
    ADMIN = 'admin'
    USER = 'user'
    GUEST = 'guest'

class UserStatus(Enum):
    """用户状态枚举"""
    ACTIVE = 'active'
    INACTIVE = 'inactive'
    BANNED = 'banned'
    PENDING = 'pending'

class User(Base):
    """用户模型"""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, comment='用户名')
    email = Column(String(255), unique=True, nullable=False, comment='邮箱')
    password_hash = Column(String(255), nullable=False, comment='密码哈希')
    role = Column(SqlEnum(UserRole), default=UserRole.USER, nullable=False, comment='用户角色')
    status = Column(SqlEnum(UserStatus), default=UserStatus.PENDING, nullable=False, comment='用户状态')
    avatar_url = Column(String(500), comment='头像URL')
    bio = Column(Text, comment='个人简介')
    last_login_at = Column(DateTime, comment='最后登录时间')
    is_active = Column(Boolean, default=True, comment='是否激活')

    def check_password(self, password):
        """验证密码"""
        # TODO: 实现密码验证逻辑
        return False

    def __repr__(self):
        return f'<User {self.username}>'
