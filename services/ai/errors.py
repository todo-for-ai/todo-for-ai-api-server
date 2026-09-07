"""AI 错误码与请求上下文（审计数据载体）。"""

from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict


class AIErrorCode(Enum):
    """AI 服务错误码"""
    SUCCESS = 0
    CONFIG_NOT_FOUND = 1001
    API_KEY_INVALID = 1002
    RATE_LIMIT_EXCEEDED = 1003
    TIMEOUT = 1004
    NETWORK_ERROR = 1005
    PARSE_ERROR = 1006
    CONTENT_FILTERED = 1007
    INSUFFICIENT_FUNDS = 1008
    MODEL_NOT_FOUND = 1009
    UNKNOWN_ERROR = 9999


@dataclass
class AIRequestContext:
    """AI 请求上下文"""
    request_id: str
    user_id: int
    user_email: str
    feature: str          # 功能标识：task_assistant, task_split, summarize
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    cache_hit: bool = False
    error_code: AIErrorCode = AIErrorCode.SUCCESS
    error_message: str = ""
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['error_code'] = self.error_code.name
        return data
