"""
Agent Secret 加密管理模块

支持多密钥版本、密钥轮换、KMS/Vault 集成
"""

import base64
import hashlib
import os
import logging
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime

logger = logging.getLogger(__name__)


class SecretEncryptionError(Exception):
    """Secret 加密错误"""
    pass


class SecretDecryptionError(Exception):
    """Secret 解密错误"""
    pass


class EncryptionKeyManager:
    """
    加密密钥管理器

    支持多密钥版本管理，实现无缝密钥轮换
    """

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if EncryptionKeyManager._initialized:
            return
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._current_key_id: Optional[str] = None
        self._load_keys_from_env()
        EncryptionKeyManager._initialized = True

    def _load_keys_from_env(self):
        """从环境变量加载密钥配置"""
        # 主密钥 - 必须设置，不再使用硬编码
        primary_key = os.environ.get('SECRET_ENCRYPTION_KEY')
        if not primary_key:
            raise SecretEncryptionError(
                "SECRET_ENCRYPTION_KEY environment variable is required. "
                "Please set a secure encryption key. "
                "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

        # 验证密钥格式
        try:
            Fernet(primary_key)
        except Exception as e:
            raise SecretEncryptionError(f"Invalid SECRET_ENCRYPTION_KEY format: {e}")

        self._keys['primary'] = {
            'key': primary_key.encode() if isinstance(primary_key, str) else primary_key,
            'created_at': datetime.utcnow(),
            'fernet': Fernet(primary_key)
        }
        self._current_key_id = 'primary'

        # 加载历史密钥（用于解密旧数据）
        for i in range(1, 5):  # 支持最多 4 个历史密钥
            key_env = os.environ.get(f'SECRET_ENCRYPTION_KEY_V{i}')
            if key_env:
                try:
                    self._keys[f'v{i}'] = {
                        'key': key_env.encode() if isinstance(key_env, str) else key_env,
                        'created_at': datetime.utcnow(),
                        'fernet': Fernet(key_env)
                    }
                    logger.info(f"Loaded historical encryption key: v{i}")
                except Exception as e:
                    logger.warning(f"Failed to load encryption key v{i}: {e}")

        logger.info(f"Encryption key manager initialized with {len(self._keys)} key(s)")

    def get_current_key(self) -> Dict[str, Any]:
        """获取当前活跃的加密密钥"""
        if not self._current_key_id or self._current_key_id not in self._keys:
            raise SecretEncryptionError("No active encryption key available")
        return self._keys[self._current_key_id]

    def get_key_by_id(self, key_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取密钥"""
        return self._keys.get(key_id)

    def encrypt(self, plaintext: str, key_id: Optional[str] = None) -> tuple[str, str]:
        """
        加密明文

        Args:
            plaintext: 要加密的明文
            key_id: 指定使用的密钥 ID，None 则使用当前密钥

        Returns:
            (加密后的密文, 使用的密钥 ID)
        """
        key_id = key_id or self._current_key_id
        key_data = self._keys.get(key_id)

        if not key_data:
            raise SecretEncryptionError(f"Encryption key '{key_id}' not found")

        try:
            encrypted = key_data['fernet'].encrypt(plaintext.encode())
            ciphertext = base64.b64encode(encrypted).decode()
            return ciphertext, key_id
        except Exception as e:
            raise SecretEncryptionError(f"Encryption failed: {e}")

    def decrypt(self, ciphertext: str, key_id: Optional[str] = None) -> str:
        """
        解密密文

        Args:
            ciphertext: 要解密的密文
            key_id: 指定使用的密钥 ID，None 则尝试所有密钥

        Returns:
            解密后的明文
        """
        try:
            encrypted_bytes = base64.b64decode(ciphertext.encode())
        except Exception as e:
            raise SecretDecryptionError(f"Invalid ciphertext format: {e}")

        # 如果指定了密钥 ID，使用该密钥
        if key_id:
            key_data = self._keys.get(key_id)
            if not key_data:
                raise SecretDecryptionError(f"Decryption key '{key_id}' not found")
            try:
                return key_data['fernet'].decrypt(encrypted_bytes).decode()
            except InvalidToken:
                raise SecretDecryptionError("Invalid token - decryption failed with specified key")

        # 未指定密钥 ID，尝试所有密钥（从新到旧）
        # 优先尝试当前密钥
        key_order = [self._current_key_id] + [k for k in self._keys.keys() if k != self._current_key_id]

        for kid in key_order:
            if kid not in self._keys:
                continue
            try:
                result = self._keys[kid]['fernet'].decrypt(encrypted_bytes).decode()
                # 如果成功但使用的是旧密钥，建议轮换
                if kid != self._current_key_id:
                    logger.info(f"Decrypted using old key '{kid}', consider rotating this secret")
                return result
            except InvalidToken:
                continue

        raise SecretDecryptionError("Failed to decrypt - no valid key found")

    def rotate_key(self, new_key: str) -> str:
        """
        轮换到新密钥

        Args:
            new_key: 新的加密密钥

        Returns:
            旧密钥 ID，可用于备份
        """
        # 验证新密钥
        try:
            Fernet(new_key)
        except Exception as e:
            raise SecretEncryptionError(f"Invalid new key format: {e}")

        # 将当前密钥移到历史版本
        old_key_id = self._current_key_id
        if old_key_id == 'primary':
            # 找到第一个可用的历史版本槽位
            for i in range(1, 5):
                v_key = f'v{i}'
                if v_key not in self._keys:
                    self._keys[v_key] = self._keys['primary']
                    logger.info(f"Moved old primary key to {v_key}")
                    break

        # 设置新密钥为 primary
        self._keys['primary'] = {
            'key': new_key.encode() if isinstance(new_key, str) else new_key,
            'created_at': datetime.utcnow(),
            'fernet': Fernet(new_key)
        }
        self._current_key_id = 'primary'

        logger.info(f"Encryption key rotated, old key moved to {old_key_id}")
        return old_key_id

    def get_key_info(self) -> Dict[str, Any]:
        """获取当前密钥信息（不包含密钥值）"""
        return {
            'current_key_id': self._current_key_id,
            'available_keys': list(self._keys.keys()),
            'key_count': len(self._keys),
            'current_key_created_at': self._keys.get(self._current_key_id, {}).get('created_at')
        }


# 全局密钥管理器实例
_key_manager: Optional[EncryptionKeyManager] = None


def get_encryption_manager() -> EncryptionKeyManager:
    """获取加密管理器实例（单例）"""
    global _key_manager
    if _key_manager is None:
        _key_manager = EncryptionKeyManager()
    return _key_manager


def reset_encryption_manager():
    """重置加密管理器（主要用于测试）"""
    global _key_manager
    EncryptionKeyManager._initialized = False
    EncryptionKeyManager._instance = None
    _key_manager = None


class KMSSecretEncryption:
    """
    KMS/Vault 集成框架

    预留接口用于集成外部密钥管理服务
    """

    def __init__(self, provider: str, config: Dict[str, Any]):
        """
        初始化 KMS 加密

        Args:
            provider: KMS 提供商 ('aws', 'azure', 'gcp', 'hashicorp_vault')
            config: 提供商配置
        """
        self.provider = provider
        self.config = config
        self._client = None
        self._init_client()

    def _init_client(self):
        """初始化 KMS 客户端"""
        if self.provider == 'aws':
            self._init_aws_kms()
        elif self.provider == 'azure':
            self._init_azure_key_vault()
        elif self.provider == 'gcp':
            self._init_gcp_kms()
        elif self.provider == 'hashicorp_vault':
            self._init_vault()
        else:
            raise ValueError(f"Unsupported KMS provider: {self.provider}")

    def _init_aws_kms(self):
        """初始化 AWS KMS"""
        try:
            import boto3
            self._client = boto3.client('kms', region_name=self.config.get('region', 'us-east-1'))
            self._key_id = self.config.get('key_id')
            logger.info("AWS KMS client initialized")
        except ImportError:
            logger.warning("boto3 not installed, AWS KMS unavailable")
            self._client = None

    def _init_azure_key_vault(self):
        """初始化 Azure Key Vault"""
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.keys import KeyClient
            vault_url = self.config.get('vault_url')
            self._credential = DefaultAzureCredential()
            self._client = KeyClient(vault_url=vault_url, credential=self._credential)
            logger.info("Azure Key Vault client initialized")
        except ImportError:
            logger.warning("Azure SDK not installed, Azure Key Vault unavailable")
            self._client = None

    def _init_gcp_kms(self):
        """初始化 GCP KMS"""
        try:
            from google.cloud import kms_v1
            self._client = kms_v1.KeyManagementServiceClient()
            self._key_name = self.config.get('key_name')
            logger.info("GCP KMS client initialized")
        except ImportError:
            logger.warning("Google Cloud KMS SDK not installed, GCP KMS unavailable")
            self._client = None

    def _init_vault(self):
        """初始化 HashiCorp Vault"""
        try:
            import hvac
            self._client = hvac.Client(
                url=self.config.get('url'),
                token=self.config.get('token')
            )
            self._mount_point = self.config.get('mount_point', 'transit')
            self._key_name = self.config.get('key_name')
            logger.info("HashiCorp Vault client initialized")
        except ImportError:
            logger.warning("hvac not installed, HashiCorp Vault unavailable")
            self._client = None

    def encrypt(self, plaintext: str) -> str:
        """使用 KMS 加密"""
        if not self._client:
            raise SecretEncryptionError(f"{self.provider} client not initialized")

        if self.provider == 'hashicorp_vault':
            return self._vault_encrypt(plaintext)
        # AWS/Azure/GCP 实现...
        raise NotImplementedError(f"KMS encryption for {self.provider} not yet implemented")

    def decrypt(self, ciphertext: str) -> str:
        """使用 KMS 解密"""
        if not self._client:
            raise SecretDecryptionError(f"{self.provider} client not initialized")

        if self.provider == 'hashicorp_vault':
            return self._vault_decrypt(ciphertext)
        # AWS/Azure/GCP 实现...
        raise NotImplementedError(f"KMS decryption for {self.provider} not yet implemented")

    def _vault_encrypt(self, plaintext: str) -> str:
        """Vault Transit 加密"""
        import base64
        plaintext_b64 = base64.b64encode(plaintext.encode()).decode()
        result = self._client.secrets.transit.encrypt(
            name=self._key_name,
            plaintext=plaintext_b64,
            mount_point=self._mount_point
        )
        return result['data']['ciphertext']

    def _vault_decrypt(self, ciphertext: str) -> str:
        """Vault Transit 解密"""
        import base64
        result = self._client.secrets.transit.decrypt(
            name=self._key_name,
            ciphertext=ciphertext,
            mount_point=self._mount_point
        )
        plaintext_b64 = result['data']['plaintext']
        return base64.b64decode(plaintext_b64).decode()


def get_secret_encryption():
    """
    获取 Secret 加密实例

    优先使用配置的 KMS，否则使用本地 Fernet 加密
    """
    kms_provider = os.environ.get('SECRET_KMS_PROVIDER')

    if kms_provider:
        config = {
            'region': os.environ.get('SECRET_KMS_REGION'),
            'key_id': os.environ.get('SECRET_KMS_KEY_ID'),
            'vault_url': os.environ.get('SECRET_VAULT_URL'),
            'token': os.environ.get('SECRET_VAULT_TOKEN'),
            'key_name': os.environ.get('SECRET_VAULT_KEY_NAME'),
            'mount_point': os.environ.get('SECRET_VAULT_MOUNT_POINT', 'transit'),
        }
        # 过滤掉 None 值
        config = {k: v for k, v in config.items() if v is not None}
        try:
            return KMSSecretEncryption(kms_provider, config)
        except Exception as e:
            logger.warning(f"Failed to initialize KMS encryption: {e}, falling back to local encryption")

    # 使用本地加密
    return get_encryption_manager()
