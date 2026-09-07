"""LLM 调用编排：限流 → 缓存 → 请求 → 响应/错误映射 → 审计。

call() 是编排器；限流/缓存/HTTP 错误分类/成功解析拆为独立方法，便于测试。
"""

import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.ai.config import get_ai_config
from services.ai.errors import AIErrorCode, AIRequestContext
from services.ai.rate_limiter import RateLimiter
from services.ai.response_cache import AIResponseCache
from services.ai.audit_logger import AIAuditLogger


class LLMService:
    """生产级 LLM 服务（支持动态配置）"""

    def __init__(self):
        self.rate_limiter = RateLimiter()
        self.cache = AIResponseCache()
        self.audit_logger = AIAuditLogger()
        self._session = None  # 延迟创建
        self._session_lock = threading.Lock()
        self._config_version = 0  # 配置版本，用于检测配置变化

    def _get_session(self) -> requests.Session:
        """获取 HTTP session（支持配置热更新）"""
        config = get_ai_config()
        current_version = hash(frozenset(config.items()))

        with self._session_lock:
            if self._session is None or self._config_version != current_version:
                self._session = self._create_session(config)
                self._config_version = current_version
            return self._session

    def _create_session(self, config: Dict[str, Any] = None) -> requests.Session:
        """创建带重试机制的 HTTP session"""
        if config is None:
            config = get_ai_config()

        session = requests.Session()

        max_retries = config.get('max_retries', 5)
        backoff_factor = config.get('retry_backoff_factor', 1.0)
        max_wait = config.get('max_retry_wait_time', 60)

        # 计算实际退避因子，确保不超过最大等待时间
        # 退避公式: {backoff_factor} * (2 ** ({retry number} - 1))
        # 我们需要确保第 max_retries 次重试的等待时间不超过 max_wait
        adjusted_backoff = min(backoff_factor, max_wait / (2 ** max(0, max_retries - 1)))

        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=adjusted_backoff,
            status_forcelist=[408, 429, 500, 502, 503, 504],
            allowed_methods=["POST", "GET"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
            pool_block=False
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
        return f"ai-{uuid.uuid4().hex[:16]}-{int(time.time())}"

    # ── 结果构造 ──

    def _error_result(self, context: AIRequestContext, start_time: float,
                      code: AIErrorCode = None, message: str = None,
                      retry_after: int = None, **extra) -> Dict[str, Any]:
        """统一错误返回：落上下文、审计、构造结果（retry_after/extra 按需）。"""
        if code is not None:
            context.error_code = code
        if message is not None:
            context.error_message = message
        context.latency_ms = (time.time() - start_time) * 1000
        self.audit_logger.log(context)
        result = {
            'success': False,
            'error': context.error_message,
            'error_code': context.error_code.value,
            'context': context.to_dict(),
        }
        if retry_after is not None:
            result['retry_after'] = retry_after
        result.update(extra)
        return result

    # ── call() 的各阶段 ──

    def _guard_rate_limit(self, context: AIRequestContext, user_id: int,
                          feature: str) -> Optional[Dict[str, Any]]:
        """限流护栏：超限返回错误结果，放行返回 None。"""
        rate_key = f"{user_id}:{feature}"
        allowed, retry_after = self.rate_limiter.is_allowed(rate_key)
        if allowed:
            return None
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

    def _guard_cache(self, context: AIRequestContext, feature: str,
                     use_cache: bool, cache_params: Optional[Dict[str, Any]],
                     start_time: float) -> Optional[Dict[str, Any]]:
        """缓存护栏：命中返回成功结果，未命中返回 None。"""
        if not (use_cache and cache_params):
            return None
        cached = self.cache.get(feature, cache_params)
        if not cached:
            return None
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

    def _apply_http_error(self, response, context: AIRequestContext):
        """非 200 状态码 → 上下文错误码/消息映射。"""
        if response.status_code == 429:
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
                context.error_message = (
                    f"Bad request: {error_text}" if error_text else "Bad request")
        else:
            context.error_code = AIErrorCode.UNKNOWN_ERROR
            # 尝试获取错误详情
            error_detail = ""
            try:
                error_json = response.json()
                error_detail = error_json.get('error', {}).get('message', '')
            except Exception:
                error_detail = response.text[:200] if response.text else ''

            context.error_message = (
                f"API returned {response.status_code}: {error_detail}"
                if error_detail else f"API returned {response.status_code}")

    def _handle_success(self, context: AIRequestContext, feature: str, response,
                        start_time: float, use_cache: bool,
                        cache_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """200 响应：解析内容/usage，写缓存，返回成功结果。"""
        try:
            result = response.json()
        except json.JSONDecodeError as e:
            return self._error_result(
                context, start_time, AIErrorCode.PARSE_ERROR,
                f"Invalid JSON response: {str(e)}",
                raw_response=response.text[:500])

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

    def _execute_chat(self, context: AIRequestContext, feature: str,
                      messages: List[Dict[str, str]], config: Dict[str, Any],
                      start_time: float, request_id: str, use_cache: bool,
                      cache_params: Optional[Dict[str, Any]],
                      temperature: Optional[float], max_tokens: Optional[int],
                      response_format: Optional[str]) -> Dict[str, Any]:
        """发送请求并处理响应（异常向上传播给 call() 的分类处理）。"""
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

        # 发送请求
        api_url = f'{api_base}/chat/completions'
        print(f"[AI Service] Calling {api_url} with model {model}")

        # 获取AI容错配置
        resilience_config = get_ai_config()
        connect_timeout = resilience_config.get('connect_timeout', 30)
        read_timeout = resilience_config.get('read_timeout', 120)

        session = self._get_session()
        response = session.post(
            api_url,
            headers=headers,
            json=payload,
            timeout=(connect_timeout, read_timeout)  # (连接超时, 读取超时)
        )

        print(f"[AI Service] Response status: {response.status_code}")

        if response.status_code == 200:
            return self._handle_success(
                context, feature, response, start_time, use_cache, cache_params)

        self._apply_http_error(response, context)
        return self._error_result(context, start_time)

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
            blocked = self._guard_rate_limit(context, user_id, feature)
            if blocked:
                return blocked

            # 2. 检查缓存
            hit = self._guard_cache(
                context, feature, use_cache, cache_params, start_time)
            if hit is not None:
                return hit

            # 3. 获取配置
            config = self._get_config()
            if not config.get('api_key'):
                return self._error_result(
                    context, start_time,
                    AIErrorCode.CONFIG_NOT_FOUND, "LLM API key not configured")

            # 4-6. 请求与响应（异常由下方分类处理）
            return self._execute_chat(
                context, feature, messages, config, start_time, request_id,
                use_cache=use_cache, cache_params=cache_params,
                temperature=temperature, max_tokens=max_tokens,
                response_format=response_format)

        except requests.exceptions.ConnectTimeout:
            err_config = get_ai_config()
            connect_timeout = err_config.get('connect_timeout', 30)
            return self._error_result(
                context, start_time, AIErrorCode.TIMEOUT,
                f"Connection timeout: Unable to connect to LLM API within "
                f"{connect_timeout}s. Please check your network connection.",
                retry_after=5)

        except requests.exceptions.ReadTimeout:
            err_config = get_ai_config()
            read_timeout = err_config.get('read_timeout', 120)
            return self._error_result(
                context, start_time, AIErrorCode.TIMEOUT,
                f"Read timeout: LLM API response took longer than "
                f"{read_timeout}s. The model may be busy, please retry.",
                retry_after=10)

        except requests.exceptions.Timeout:
            return self._error_result(
                context, start_time, AIErrorCode.TIMEOUT,
                "Request timeout. Please retry.", retry_after=5)

        except requests.exceptions.RequestException as e:
            return self._error_result(
                context, start_time, AIErrorCode.NETWORK_ERROR,
                f"Network error: {str(e)}")

        except Exception as e:
            return self._error_result(
                context, start_time, AIErrorCode.UNKNOWN_ERROR,
                f"Unexpected error: {str(e)}")


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
