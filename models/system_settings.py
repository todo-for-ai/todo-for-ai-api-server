#!/usr/bin/env python3
"""
系统设置模型

用于存储系统级别的配置，如大模型 API 配置
"""

from sqlalchemy import Column, Integer, String, DateTime, Text, JSON
from sqlalchemy.orm import relationship
from models.base import BaseModel
from datetime import datetime


class SystemSettings(BaseModel):
    """系统设置模型"""

    __tablename__ = 'system_settings'

    # 设置键（唯一标识）
    key = Column(String(100), nullable=False, unique=True, comment='设置键')

    # 设置值（JSON格式存储复杂数据）
    value = Column(JSON, nullable=True, comment='设置值（JSON格式）')

    # 设置描述
    description = Column(Text, comment='设置描述')

    # 最后更新时间
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='最后更新时间')

    # 最后更新者（用户ID）
    updated_by = Column(Integer, comment='最后更新者ID')

    def __repr__(self):
        return f'<SystemSettings {self.key}: {self.value}>'

    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'updated_by': self.updated_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def get_setting(cls, key, default=None):
        """获取指定设置"""
        setting = cls.query.filter_by(key=key).first()
        if setting:
            return setting.value
        return default

    @classmethod
    def set_setting(cls, key, value, description=None, updated_by=None):
        """设置指定配置"""
        setting = cls.query.filter_by(key=key).first()
        if setting:
            setting.value = value
            if description:
                setting.description = description
            setting.updated_by = updated_by
            setting.save()
        else:
            setting = cls(
                key=key,
                value=value,
                description=description,
                updated_by=updated_by
            )
            setting.save()
        return setting

    @classmethod
    def get_llm_config(cls):
        """获取大模型 API 配置"""
        return cls.get_setting('llm_config', {
            'provider': 'openai',
            'api_base': '',
            'api_key': '',
            'model': 'gpt-4',
            'temperature': 0.7,
            'max_tokens': 2000,
            'timeout': 60
        })

    @classmethod
    def set_llm_config(cls, config, updated_by=None):
        """设置大模型 API 配置"""
        return cls.set_setting(
            'llm_config',
            config,
            description='大模型 API 配置，用于平台所有 AI 功能',
            updated_by=updated_by
        )

    @classmethod
    def get_all_settings(cls):
        """获取所有系统设置"""
        settings = cls.query.all()
        return {s.key: s.to_dict() for s in settings}
