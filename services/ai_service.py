"""
AI 服务基础设施
生产级别的 LLM 调用封装，包含重试、限流、缓存、审计等功能
"""

import time
import json
import hashlib
import functools
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 配置常量
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 0.5
RATE_LIMIT_REQUESTS = 60  # 每分钟最大请求数
RATE_LIMIT_WINDOW = 60    # 窗口大小（秒）
CACHE_TTL = 300          # 缓存时间（秒）


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


class RateLimiter:
    """滑动窗口限流器"""

    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS,
                 window_size: int = RATE_LIMIT_WINDOW):
        self.max_requests = max_requests
        self.window_size = window_size
        self.requests: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> tuple[bool, int]:
        """
        检查是否允许请求
        返回: (是否允许, 剩余配额)
        """
        now = time.time()
        window_start = now - self.window_size

        with self._lock:
            # 清理过期请求
            if key in self.requests:
                self.requests[key] = [
                    ts for ts in self.requests[key] if ts > window_start
                ]
            else:
                self.requests[key] = []

            # 检查配额
            current_count = len(self.requests[key])
            if current_count >= self.max_requests:
                retry_after = int(self.requests[key][0] + self.window_size - now)
                return False, retry_after

            # 记录请求
            self.requests[key].append(now)
            remaining = self.max_requests - current_count - 1
            return True, remaining

    def get_stats(self, key: str) -> Dict[str, Any]:
        """获取限流统计"""
        now = time.time()
        window_start = now - self.window_size

        with self._lock:
            if key not in self.requests:
                return {"current": 0, "remaining": self.max_requests}

            valid_requests = [
                ts for ts in self.requests[key] if ts > window_start
            ]
            return {
                "current": len(valid_requests),
                "remaining": self.max_requests - len(valid_requests),
                "window_size": self.window_size
            }


class AIResponseCache:
    """AI 响应缓存（内存 + Redis 双级缓存）"""

    def __init__(self, ttl: int = CACHE_TTL):
        self.ttl = ttl
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _generate_key(self, feature: str, params: Dict[str, Any]) -> str:
        """生成缓存 key"""
        key_data = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return f"ai:{feature}:{hashlib.sha256(key_data.encode()).hexdigest()[:32]}"

    def get(self, feature: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """获取缓存"""
        key = self._generate_key(feature, params)

        with self._lock:
            if key in self._memory_cache:
                entry = self._memory_cache[key]
                if entry['expires_at'] > time.time():
                    entry['hits'] += 1
                    return entry['data']
                else:
                    del self._memory_cache[key]

        # 尝试从 Redis 获取（如果可用）
        try:
            from core.redis_client import get_json
            data = get_json(key)
            if data:
                # 回填内存缓存
                with self._lock:
                    self._memory_cache[key] = {
                        'data': data,
                        'expires_at': time.time() + self.ttl,
                        'hits': 1
                    }
                return data
        except Exception:
            pass

        return None

    def set(self, feature: str, params: Dict[str, Any], data: Dict[str, Any]):
        """设置缓存"""
        key = self._generate_key(feature, params)

        with self._lock:
            self._memory_cache[key] = {
                'data': data,
                'expires_at': time.time() + self.ttl,
                'hits': 0
            }

        # 写入 Redis（如果可用）
        try:
            from core.redis_client import set_json
            set_json(key, data, self.ttl)
        except Exception:
            pass

    def invalidate(self, feature: str = None):
        """清除缓存"""
        with self._lock:
            if feature:
                keys_to_remove = [
                    k for k in self._memory_cache.keys()
                    if k.startswith(f"ai:{feature}:")
                ]
                for k in keys_to_remove:
                    del self._memory_cache[k]
            else:
                self._memory_cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        with self._lock:
            total = len(self._memory_cache)
            expired = sum(
                1 for e in self._memory_cache.values()
                if e['expires_at'] <= time.time()
            )
            hits = sum(e['hits'] for e in self._memory_cache.values())
            return {
                "total_entries": total,
                "expired_entries": expired,
                "total_hits": hits
            }


class AIAuditLogger:
    """AI 审计日志记录器"""

    def __init__(self):
        self._buffer: List[AIRequestContext] = []
        self._lock = threading.Lock()
        self._flush_interval = 10  # 每10秒刷新
        self._start_flush_timer()

    def _start_flush_timer(self):
        """启动定时刷新"""
        def flush_periodically():
            while True:
                time.sleep(self._flush_interval)
                self.flush()

        thread = threading.Thread(target=flush_periodically, daemon=True)
        thread.start()

    def log(self, context: AIRequestContext):
        """记录请求上下文"""
        with self._lock:
            self._buffer.append(context)

        # 如果缓冲区太大，立即刷新
        if len(self._buffer) >= 100:
            self.flush()

    def flush(self):
        """刷新日志到数据库"""
        if not self._buffer:
            return

        with self._lock:
            logs_to_save = self._buffer.copy()
            self._buffer.clear()

        try:
            # 异步保存到数据库
            from models import db, AIRequestLog

            for ctx in logs_to_save:
                log_entry = AIRequestLog(
                    request_id=ctx.request_id,
                    user_id=ctx.user_id,
                    user_email=ctx.user_email,
                    feature=ctx.feature,
                    prompt_tokens=ctx.prompt_tokens,
                    completion_tokens=ctx.completion_tokens,
                    total_tokens=ctx.total_tokens,
                    latency_ms=ctx.latency_ms,
                    cache_hit=ctx.cache_hit,
                    error_code=ctx.error_code.value,
                    error_message=ctx.error_message,
                    created_at=ctx.created_at
                )
                db.session.add(log_entry)

            db.session.commit()
        except Exception as e:
            # 保存失败时，打印错误但不抛出
            print(f"[AI Audit] Failed to save logs: {e}")
            # 重新放入缓冲区
            with self._lock:
                self._buffer.extend(logs_to_save)


class LLMService:
    """生产级 LLM 服务"""

    def __init__(self):
        self.rate_limiter = RateLimiter()
        self.cache = AIResponseCache()
        self.audit_logger = AIAuditLogger()
        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        """创建带重试机制的 HTTP session"""
        session = requests.Session()

        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=RETRY_BACKOFF_FACTOR,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST", "GET"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )

        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def _get_config(self) -> Dict[str, Any]:
        """获取 LLM 配置"""
        try:
            from models import SystemSettings
            return SystemSettings.get_llm_config()
        except Exception:
            # 使用默认配置
            return {
                'provider': 'openai',
                'api_base': 'https://api.openai.com/v1',
                'api_key': '',
                'model': 'gpt-4',
                'temperature': 0.7,
                'max_tokens': 2000
            }

    def _generate_request_id(self) -> str:
        """生成请求 ID"""
        import uuid
        return f"ai-{uuid.uuid4().hex[:16]}-{int(time.time())}"

    def call(self,
             feature: str,
             messages: List[Dict[str, str]],
             user_id: int = 0,
             user_email: str = "",
             use_cache: bool = True,
             cache_params: Optional[Dict[str, Any]] = None,
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None,
             response_format: Optional[str] = None) -> Dict[str, Any]:
        """
        调用 LLM

        Args:
            feature: 功能标识
            messages: 消息列表
            user_id: 用户ID
            user_email: 用户邮箱
            use_cache: 是否使用缓存
            cache_params: 缓存参数
            temperature: 温度
            max_tokens: 最大token数
            response_format: 响应格式 (json/text)

        Returns:
            包含 success, data, error, context 的字典
        """
        request_id = self._generate_request_id()
        start_time = time.time()

        context = AIRequestContext(
            request_id=request_id,
            user_id=user_id,
            user_email=user_email,
            feature=feature
        )

        try:
            # 1. 限流检查
            rate_key = f"{user_id}:{feature}"
            allowed, retry_after = self.rate_limiter.is_allowed(rate_key)
            if not allowed:
                context.error_code = AIErrorCode.RATE_LIMIT_EXCEEDED
                context.error_message = f"Rate limit exceeded. Retry after {retry_after}s"
                self.audit_logger.log(context)
                return {
                    'success': False,
                    'error': context.error_message,
                    'error_code': AIErrorCode.RATE_LIMIT_EXCEEDED.value,
                    'retry_after': retry_after,
                    'context': context.to_dict()
                }

            # 2. 检查缓存
            if use_cache and cache_params:
                cached = self.cache.get(feature, cache_params)
                if cached:
                    context.cache_hit = True
                    context.latency_ms = (time.time() - start_time) * 1000
                    self.audit_logger.log(context)
                    # cached is {'content': actual_content}
                    content = cached.get('content', cached) if isinstance(cached, dict) else cached
                    return {
                        'success': True,
                        'data': content,
                        'cached': True,
                        'context': context.to_dict()
                    }

            # 3. 获取配置
            config = self._get_config()
            if not config.get('api_key'):
                context.error_code = AIErrorCode.CONFIG_NOT_FOUND
                context.error_message = "LLM API key not configured"
                self.audit_logger.log(context)
                return {
                    'success': False,
                    'error': context.error_message,
                    'error_code': AIErrorCode.CONFIG_NOT_FOUND.value,
                    'context': context.to_dict()
                }

            # 4. 准备请求
            api_base = config.get('api_base', 'https://api.openai.com/v1').rstrip('/')
            api_key = config.get('api_key', '')
            model = config.get('model', 'gpt-4')
            temp = temperature or config.get('temperature', 0.7)
            max_tok = max_tokens or config.get('max_tokens', 2000)

            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'X-Request-ID': request_id
            }

            payload = {
                'model': model,
                'messages': messages,
                'temperature': temp,
                'max_tokens': max_tok,
                'stream': False
            }

            if response_format == 'json':
                payload['response_format'] = {'type': 'json_object'}

            # 5. 发送请求
            api_url = f'{api_base}/chat/completions'
            print(f"[AI Service] Calling {api_url} with model {model}")

            response = self._session.post(
                api_url,
                headers=headers,
                json=payload,
                timeout=DEFAULT_TIMEOUT
            )

            print(f"[AI Service] Response status: {response.status_code}")

            # 6. 处理响应
            if response.status_code == 200:
                # 尝试解析 JSON 响应
                try:
                    result = response.json()
                except json.JSONDecodeError as e:
                    context.error_code = AIErrorCode.PARSE_ERROR
                    context.error_message = f"Invalid JSON response: {str(e)}"
                    context.latency_ms = (time.time() - start_time) * 1000
                    self.audit_logger.log(context)
                    return {
                        'success': False,
                        'error': context.error_message,
                        'error_code': AIErrorCode.PARSE_ERROR.value,
                        'raw_response': response.text[:500],
                        'context': context.to_dict()
                    }

                content = result.get('choices', [{}])[0].get('message', {}).get('content', '')

                # 更新上下文
                usage = result.get('usage', {})
                context.prompt_tokens = usage.get('prompt_tokens', 0)
                context.completion_tokens = usage.get('completion_tokens', 0)
                context.total_tokens = usage.get('total_tokens', 0)
                context.latency_ms = (time.time() - start_time) * 1000
                context.error_code = AIErrorCode.SUCCESS

                # 写入缓存
                if use_cache and cache_params:
                    self.cache.set(feature, cache_params, {'content': content})

                self.audit_logger.log(context)

                return {
                    'success': True,
                    'data': content,
                    'usage': usage,
                    'model': result.get('model'),
                    'context': context.to_dict()
                }

            elif response.status_code == 429:
                context.error_code = AIErrorCode.RATE_LIMIT_EXCEEDED
                context.error_message = "Provider rate limit exceeded"
            elif response.status_code == 401:
                context.error_code = AIErrorCode.API_KEY_INVALID
                context.error_message = "Invalid API key"
            elif response.status_code == 400:
                # 尝试解析错误响应，处理非 JSON 响应的情况
                error_data = {}
                error_text = ""
                try:
                    error_data = response.json()
                except json.JSONDecodeError:
                    error_text = response.text[:200] if response.text else "Bad request"

                if 'insufficient_quota' in str(error_data) or 'insufficient_quota' in error_text:
                    context.error_code = AIErrorCode.INSUFFICIENT_FUNDS
                    context.error_message = "Insufficient quota"
                elif error_data and 'error' in error_data:
                    context.error_code = AIErrorCode.UNKNOWN_ERROR
                    context.error_message = error_data.get('error', {}).get('message', 'Bad request')
                else:
                    context.error_code = AIErrorCode.UNKNOWN_ERROR
                    context.error_message = f"Bad request: {error_text}" if error_text else "Bad request"
            else:
                context.error_code = AIErrorCode.UNKNOWN_ERROR
                # 尝试获取错误详情
                error_detail = ""
                try:
                    error_json = response.json()
                    error_detail = error_json.get('error', {}).get('message', '')
                except:
                    error_detail = response.text[:200] if response.text else ''

                context.error_message = f"API returned {response.status_code}: {error_detail}" if error_detail else f"API returned {response.status_code}"

            context.latency_ms = (time.time() - start_time) * 1000
            self.audit_logger.log(context)

            return {
                'success': False,
                'error': context.error_message,
                'error_code': context.error_code.value,
                'context': context.to_dict()
            }

        except requests.exceptions.Timeout:
            context.error_code = AIErrorCode.TIMEOUT
            context.error_message = f"Request timeout after {DEFAULT_TIMEOUT}s"
            context.latency_ms = (time.time() - start_time) * 1000
            self.audit_logger.log(context)
            return {
                'success': False,
                'error': context.error_message,
                'error_code': AIErrorCode.TIMEOUT.value,
                'context': context.to_dict()
            }

        except requests.exceptions.RequestException as e:
            context.error_code = AIErrorCode.NETWORK_ERROR
            context.error_message = f"Network error: {str(e)}"
            context.latency_ms = (time.time() - start_time) * 1000
            self.audit_logger.log(context)
            return {
                'success': False,
                'error': context.error_message,
                'error_code': AIErrorCode.NETWORK_ERROR.value,
                'context': context.to_dict()
            }

        except Exception as e:
            context.error_code = AIErrorCode.UNKNOWN_ERROR
            context.error_message = f"Unexpected error: {str(e)}"
            context.latency_ms = (time.time() - start_time) * 1000
            self.audit_logger.log(context)
            return {
                'success': False,
                'error': context.error_message,
                'error_code': AIErrorCode.UNKNOWN_ERROR.value,
                'context': context.to_dict()
            }


# 全局 LLM 服务实例
llm_service = LLMService()


def call_llm_production(feature: str,
                       messages: List[Dict[str, str]],
                       user_id: int = 0,
                       user_email: str = "",
                       use_cache: bool = True,
                       cache_params: Optional[Dict[str, Any]] = None,
                       **kwargs) -> Dict[str, Any]:
    """
    生产级 LLM 调用接口
    """
    return llm_service.call(
        feature=feature,
        messages=messages,
        user_id=user_id,
        user_email=user_email,
        use_cache=use_cache,
        cache_params=cache_params,
        **kwargs
    )
