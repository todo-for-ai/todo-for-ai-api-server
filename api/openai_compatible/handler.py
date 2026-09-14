"""OpenAI 请求处理器：请求校验、响应/流式块构造、异步请求日志（原样搬移）。"""

import time
import json
import uuid
from typing import Dict

from models import db
from api.openai_compatible._core import MAX_MESSAGES_LENGTH


class OpenAIRequestHandler:
    """OpenAI 请求处理器"""

    def __init__(self):
        self.request_id = None
        self.start_time = None

    def validate_chat_request(self, data: Dict) -> tuple[bool, str]:
        """验证 chat/completions 请求"""
        # 检查必需字段
        if 'messages' not in data:
            return False, "Missing required field: messages"

        messages = data.get('messages', [])
        if not isinstance(messages, list) or len(messages) == 0:
            return False, "messages must be a non-empty list"

        if len(messages) > MAX_MESSAGES_LENGTH:
            return False, f"messages cannot exceed {MAX_MESSAGES_LENGTH} items"

        # 验证消息格式
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return False, f"Message at index {i} must be an object"
            if 'role' not in msg:
                return False, f"Message at index {i} missing 'role' field"
            if 'content' not in msg:
                return False, f"Message at index {i} missing 'content' field"
            if msg['role'] not in ['system', 'user', 'assistant', 'tool']:
                return False, f"Invalid role at index {i}: {msg['role']}"

        # 验证模型
        model = data.get('model', '')
        if not model:
            return False, "model is required"

        # 验证参数范围
        temperature = data.get('temperature')
        if temperature is not None and (temperature < 0 or temperature > 2):
            return False, "temperature must be between 0 and 2"

        max_tokens = data.get('max_tokens')
        if max_tokens is not None and (max_tokens < 1 or max_tokens > 32000):
            return False, "max_tokens must be between 1 and 32000"

        # 验证 OpenAI 标准额外参数
        top_p = data.get('top_p')
        if top_p is not None and (top_p < 0 or top_p > 1):
            return False, "top_p must be between 0 and 1"

        presence_penalty = data.get('presence_penalty')
        if presence_penalty is not None and (presence_penalty < -2 or presence_penalty > 2):
            return False, "presence_penalty must be between -2 and 2"

        frequency_penalty = data.get('frequency_penalty')
        if frequency_penalty is not None and (frequency_penalty < -2 or frequency_penalty > 2):
            return False, "frequency_penalty must be between -2 and 2"

        n = data.get('n')
        if n is not None and n != 1:
            return False, "Currently only n=1 is supported"

        # 验证 stop 序列
        stop = data.get('stop')
        if stop is not None:
            if isinstance(stop, str):
                if len(stop) > 500:
                    return False, "stop sequence too long (max 500 characters)"
            elif isinstance(stop, list):
                if len(stop) > 4:
                    return False, "maximum 4 stop sequences allowed"
                for s in stop:
                    if len(s) > 500:
                        return False, "stop sequence too long (max 500 characters)"

        return True, ""

    def build_cache_params(self, data: Dict) -> Dict:
        """构建缓存参数"""
        # 提取用于缓存的字段
        cache_fields = {
            'model': data.get('model'),
            'messages': data.get('messages', []),
            'temperature': data.get('temperature', 0.7),
            'max_tokens': data.get('max_tokens', 2000),
            'top_p': data.get('top_p', 1.0),
        }
        return cache_fields

    def generate_request_id(self) -> str:
        """生成请求ID"""
        return f"chatcmpl-{uuid.uuid4().hex[:24]}"

    def create_response(self, content: str, model: str, usage: Dict = None) -> Dict:
        """创建 OpenAI 格式的响应"""
        now = int(time.time())
        self.request_id = self.request_id or self.generate_request_id()

        if usage is None:
            # 估算token使用量
            prompt_tokens = len(content) // 4  # 粗略估算
            completion_tokens = len(content) // 4
            total_tokens = prompt_tokens + completion_tokens
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens
            }

        return {
            "id": self.request_id,
            "object": "chat.completion",
            "created": now,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "logprobs": None,
                    "finish_reason": "stop"
                }
            ],
            "usage": usage,
            "system_fingerprint": None
        }

    def create_stream_chunk(self, content: str, model: str, finish_reason: str = None) -> str:
        """创建流式响应块"""
        self.request_id = self.request_id or self.generate_request_id()

        if finish_reason:
            data = {
                "id": self.request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "logprobs": None,
                        "finish_reason": finish_reason
                    }
                ]
            }
        else:
            data = {
                "id": self.request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": content
                        },
                        "logprobs": None,
                        "finish_reason": None
                    }
                ]
            }

        return f"data: {json.dumps(data)}\n\n"

    def log_request(self, user_id: int, feature: str, params: Dict, latency_ms: float, cache_hit: bool = False):
        """记录请求日志 (异步)"""
        try:
            from models.ai_request_log import AIRequestLog

            # 异步记录，不阻塞响应
            def _log():
                try:
                    log_entry = AIRequestLog(
                        request_id=self.request_id or str(uuid.uuid4()),
                        user_id=user_id,
                        user_email="",  # 可以从user对象获取
                        feature=feature,
                        latency_ms=latency_ms,
                        cache_hit=cache_hit,
                        error_code=0
                    )
                    db.session.add(log_entry)
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            # 使用线程异步记录
            import threading
            threading.Thread(target=_log, daemon=True).start()

        except Exception:
            pass
