#!/usr/bin/env python3
"""
系统设置模型

用于存储系统级别的配置，如大模型 API 配置、AI 容错配置
支持敏感配置的加密存储
"""

import json
import logging
from sqlalchemy import Column, Integer, String, DateTime, Text, JSON
from sqlalchemy.orm import relationship
from models.base import BaseModel
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


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

    # 是否为加密存储
    is_encrypted = Column(Integer, default=0, comment='是否加密存储（1=是，0=否）')

    # 加密密钥版本（用于密钥轮换）
    key_version = Column(String(50), nullable=True, comment='加密密钥版本')

    def __repr__(self):
        return f'<SystemSettings {self.key}: {self.value}>'

    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'description': self.description,
            'is_encrypted': bool(self.is_encrypted),
            'key_version': self.key_version,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'updated_by': self.updated_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def _get_encryption_manager(cls):
        """获取加密管理器（延迟加载，支持应用上下文外使用）"""
        try:
            from core.secret_encryption import get_encryption_manager
            return get_encryption_manager()
        except Exception as e:
            logger.error(f"Failed to initialize encryption manager: {e}")
            raise

    @classmethod
    def get_setting(cls, key, default=None):
        """获取指定设置（不自动解密）"""
        setting = cls.query.filter_by(key=key).first()
        if setting:
            return setting.value
        return default

    @classmethod
    def get_setting_encrypted(cls, key, default=None):
        """获取加密设置并自动解密"""
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            return default

        value = setting.value
        if setting.is_encrypted and isinstance(value, str):
            # 需要解密
            try:
                encryptor = cls._get_encryption_manager()
                decrypted = encryptor.decrypt(value)
                return json.loads(decrypted)
            except Exception as e:
                logger.error(f"Failed to decrypt setting {key}: {e}")
                return default
        return value

    @classmethod
    def set_setting(cls, key, value, description=None, updated_by=None, encrypt=False):
        """设置指定配置"""
        if encrypt:
            # 加密存储
            try:
                encryptor = cls._get_encryption_manager()
                plaintext = json.dumps(value)
                ciphertext, key_id = encryptor.encrypt(plaintext)
                value = ciphertext
            except Exception as e:
                logger.error(f"Failed to encrypt setting {key}: {e}")
                raise

        setting = cls.query.filter_by(key=key).first()
        if setting:
            setting.value = value
            setting.is_encrypted = 1 if encrypt else 0
            setting.key_version = key_id if encrypt else None
            if description:
                setting.description = description
            setting.updated_by = updated_by
            setting.save()
        else:
            setting = cls(
                key=key,
                value=value,
                description=description,
                updated_by=updated_by,
                is_encrypted=1 if encrypt else 0,
                key_version=key_id if encrypt else None
            )
            setting.save()
        return setting

    @classmethod
    def get_llm_config(cls):
        """获取大模型 API 配置（自动解密）"""
        return cls.get_setting_encrypted('llm_config', {
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
        """设置大模型 API 配置（API密钥加密存储）"""
        # 确保 api_key 被加密存储
        return cls.set_setting(
            'llm_config',
            config,
            description='大模型 API 配置，用于平台所有 AI 功能',
            updated_by=updated_by,
            encrypt=True  # 加密存储
        )

    @classmethod
    def get_ai_resilience_config(cls):
        """
        获取 AI 容错配置

        返回配置项：
        - connect_timeout: 连接超时（秒）
        - read_timeout: 读取超时（秒）
        - max_retries: 最大重试次数
        - retry_backoff_factor: 指数退避因子
        - max_retry_wait_time: 最大重试等待时间（秒）
        - rate_limit_requests: 每分钟最大请求数
        - rate_limit_window: 限流窗口大小（秒）
        - cache_ttl: 缓存时间（秒）
        """
        default_config = {
            'connect_timeout': 30,
            'read_timeout': 120,
            'max_retries': 5,
            'retry_backoff_factor': 1.0,
            'max_retry_wait_time': 60,
            'rate_limit_requests': 60,
            'rate_limit_window': 60,
            'cache_ttl': 300
        }
        return cls.get_setting('ai_resilience_config', default_config)

    @classmethod
    def set_ai_resilience_config(cls, config, updated_by=None):
        """
        设置 AI 容错配置

        Args:
            config: 配置字典
            updated_by: 更新者用户ID
        """
        # 验证配置值
        validated_config = cls._validate_ai_resilience_config(config)

        # 保存配置
        result = cls.set_setting(
            'ai_resilience_config',
            validated_config,
            description='AI 容错配置：超时、重试、限流等参数',
            updated_by=updated_by,
            encrypt=False  # 这些配置不需要加密
        )

        # 使AI服务配置缓存失效
        try:
            from services.ai_service import invalidate_ai_config_cache
            invalidate_ai_config_cache()
        except Exception:
            # 忽略缓存失效错误
            pass

        return result

    @classmethod
    def _validate_ai_resilience_config(cls, config):
        """验证并清理 AI 容错配置"""
        validated = {}

        # 连接超时：1-300秒
        connect_timeout = config.get('connect_timeout', 30)
        validated['connect_timeout'] = max(1, min(300, int(connect_timeout)))

        # 读取超时：1-600秒
        read_timeout = config.get('read_timeout', 120)
        validated['read_timeout'] = max(1, min(600, int(read_timeout)))

        # 最大重试次数：0-10次
        max_retries = config.get('max_retries', 5)
        validated['max_retries'] = max(0, min(10, int(max_retries)))

        # 指数退避因子：0.1-5.0
        backoff_factor = config.get('retry_backoff_factor', 1.0)
        validated['retry_backoff_factor'] = max(0.1, min(5.0, float(backoff_factor)))

        # 最大重试等待时间：1-300秒
        max_wait = config.get('max_retry_wait_time', 60)
        validated['max_retry_wait_time'] = max(1, min(300, int(max_wait)))

        # 限流请求数：1-1000
        rate_limit = config.get('rate_limit_requests', 60)
        validated['rate_limit_requests'] = max(1, min(1000, int(rate_limit)))

        # 限流窗口：1-3600秒
        rate_window = config.get('rate_limit_window', 60)
        validated['rate_limit_window'] = max(1, min(3600, int(rate_window)))

        # 缓存时间：0-3600秒
        cache_ttl = config.get('cache_ttl', 300)
        validated['cache_ttl'] = max(0, min(3600, int(cache_ttl)))

        return validated

    @classmethod
    def migrate_to_encrypted(cls, key, updated_by=None):
        """
        将现有明文配置迁移为加密存储

        Args:
            key: 配置键
            updated_by: 更新者用户ID

        Returns:
            (成功, 消息)
        """
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            return False, f"Setting {key} not found"

        if setting.is_encrypted:
            return True, f"Setting {key} is already encrypted"

        try:
            # 加密现有值
            value = setting.value
            if value is None:
                return False, f"Setting {key} has no value"

            encryptor = cls._get_encryption_manager()
            plaintext = json.dumps(value)
            ciphertext, key_id = encryptor.encrypt(plaintext)

            # 更新记录
            setting.value = ciphertext
            setting.is_encrypted = 1
            setting.key_version = key_id
            setting.updated_by = updated_by
            setting.save()

            logger.info(f"Successfully migrated setting {key} to encrypted storage")
            return True, f"Setting {key} migrated successfully"
        except Exception as e:
            logger.error(f"Failed to migrate setting {key}: {e}")
            return False, f"Failed to migrate: {str(e)}"

    @classmethod
    def get_all_settings(cls, include_encrypted=False):
        """获取所有系统设置"""
        settings = cls.query.all()
        result = {}
        for s in settings:
            data = s.to_dict()
            # 如果包含加密值，不解密
            if not include_encrypted and s.is_encrypted:
                data['value'] = '[encrypted]'
            result[s.key] = data
        return result

    @classmethod
    def rotate_encryption(cls, key, updated_by=None):
        """
        轮换配置加密密钥

        Args:
            key: 配置键
            updated_by: 更新者用户ID

        Returns:
            (成功, 消息)
        """
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            return False, f"Setting {key} not found"

        if not setting.is_encrypted:
            return False, f"Setting {key} is not encrypted"

        try:
            # 先解密
            encryptor = cls._get_encryption_manager()
            decrypted = encryptor.decrypt(setting.value)
            value = json.loads(decrypted)

            # 重新加密（使用新密钥）
            plaintext = json.dumps(value)
            ciphertext, key_id = encryptor.encrypt(plaintext)

            # 更新
            setting.value = ciphertext
            setting.key_version = key_id
            setting.updated_by = updated_by
            setting.save()

            logger.info(f"Successfully rotated encryption for setting {key}")
            return True, f"Setting {key} encryption rotated successfully"
        except Exception as e:
            logger.error(f"Failed to rotate encryption for setting {key}: {e}")
            return False, f"Failed to rotate: {str(e)}"
