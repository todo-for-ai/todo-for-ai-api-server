"""
OpenAI API 兼容路由

提供与 OpenAI API 兼容的接口，支持：
- /v1/chat/completions
- /v1/models
- /v1/embeddings
- 流式响应 (streaming)
- 自定义认证机制

高并发优化：
- Redis 缓存层
- 连接池复用
- 异步处理
"""

import time
import json
import uuid
import hashlib
from typing import Dict, Any, Optional, List
from functools import wraps
from flask import Blueprint, request, Response, stream_with_context, g, jsonify
from datetime import datetime

from api.base import ApiResponse, validate_json_request
from core.auth import unified_auth_required, get_current_user, get_current_token
from core.redis_client import get_redis_client, get_json, set_json
from models import db, AgentSession

openai_bp = Blueprint('openai_compatible', __name__)


def openai_auth_required(f):
    """
    OpenAI API 认证装饰器 - 支持多种认证方式：
    1. API Token
    2. JWT
    3. Agent Session Token
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return ApiResponse.unauthorized('Authentication required').to_response()

        token = auth_header.split(' ')[1]

        # 1. 尝试 API Token 认证
        from models import ApiToken
        api_token = ApiToken.verify_token(token)
        if api_token:
            g.current_user = api_token.user
            g.current_token = api_token
            g.auth_method = 'api_token'
            return f(*args, **kwargs)

        # 2. 尝试 Agent Session 认证
        session = AgentSession.verify_session_token(token)
        if session:
            from models import Agent
            agent = Agent.query.get(session.agent_id)
            if agent and agent.status and agent.status.value == 'active':
                g.current_agent = agent
                g.current_agent_session = session
                g.auth_method = 'agent_session'
                return f(*args, **kwargs)

        # 3. 尝试 JWT 认证
        try:
            from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            if user_id:
                from models import User
                user = User.query.get(user_id)
                if user and user.is_active():
                    g.current_user = user
                    g.auth_method = 'jwt'
                    return f(*args, **kwargs)
        except Exception:
            pass

        return ApiResponse.unauthorized('Authentication required').to_response()

    return decorated_function


# ============== 常量配置 ==============

# 缓存配置
CACHE_TTL_SECONDS = 300  # 5分钟缓存
CACHE_KEY_PREFIX = "openai:"
CACHE_CONSISTENCY_LOCK_PREFIX = "lock:openai:"
CACHE_INVALIDATION_CHANNEL = "openai:cache:invalidate"

# 请求限制
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10MB
MAX_MESSAGES_LENGTH = 100  # 最大消息数
MAX_PROMPT_LENGTH = 100000  # 最大prompt长度

# 支持的模型列表
SUPPORTED_MODELS = [
    {"id": "gpt-4", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-4-turbo", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-3.5-turbo", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-3.5-turbo-16k", "object": "model", "created": 1677610602, "owned_by": "openai"},
]


# ============== 缓存管理器 (支持缓存一致性) ==============

class OpenAICacheManager:
    """
    OpenAI API 缓存管理器

    特性：
    - 双级缓存 (Redis + 内存)
    - 缓存一致性保障 (分布式锁、失效广播)
    - 缓存穿透保护
    """

    def __init__(self):
        self._local_cache: Dict[str, Dict] = {}
        self._local_cache_ttl = 60  # 本地缓存60秒

    def _generate_cache_key(self, feature: str, params: Dict) -> str:
        """生成缓存key"""
        key_data = json.dumps(params, sort_keys=True, ensure_ascii=False)
        hash_val = hashlib.sha256(key_data.encode()).hexdigest()[:32]
        return f"{CACHE_KEY_PREFIX}{feature}:{hash_val}"

    def _acquire_lock(self, key: str, timeout: int = 10) -> bool:
        """获取分布式锁 (防止缓存击穿)"""
        redis_client = get_redis_client()
        if not redis_client:
            return True  # Redis不可用，跳过锁

        lock_key = f"{CACHE_CONSISTENCY_LOCK_PREFIX}{key}"
        lock_value = str(uuid.uuid4())

        try:
            # NX: 只有key不存在时才设置, EX: 设置过期时间
            acquired = redis_client.set(lock_key, lock_value, nx=True, ex=timeout)
            if acquired:
                # 将锁值存入 g，用于释放
                g.cache_lock_value = lock_value
                return True
            return False
        except Exception:
            return True  # 出错时允许继续

    def _release_lock(self, key: str):
        """释放分布式锁"""
        redis_client = get_redis_client()
        if not redis_client:
            return

        lock_key = f"{CACHE_CONSISTENCY_LOCK_PREFIX}{key}"
        lock_value = getattr(g, 'cache_lock_value', None)

        if not lock_value:
            return

        try:
            # 使用Lua脚本确保原子性释放
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            redis_client.eval(lua_script, 1, lock_key, lock_value)
        except Exception:
            pass

    def get(self, feature: str, params: Dict) -> Optional[Dict]:
        """
        获取缓存

        策略：
        1. 先查本地缓存
        2. 再查Redis缓存
        3. 返回并回填本地缓存
        """
        cache_key = self._generate_cache_key(feature, params)
        now = time.time()

        # 1. 检查本地缓存
        if cache_key in self._local_cache:
            entry = self._local_cache[cache_key]
            if entry['expires_at'] > now:
                entry['hits'] += 1
                return entry['data']
            else:
                del self._local_cache[cache_key]

        # 2. 检查Redis缓存
        try:
            data = get_json(cache_key)
            if data:
                # 回填本地缓存
                self._local_cache[cache_key] = {
                    'data': data,
                    'expires_at': now + self._local_cache_ttl,
                    'hits': 1
                }
                return data
        except Exception:
            pass

        return None

    def set(self, feature: str, params: Dict, data: Dict, ttl: int = None):
        """
        设置缓存

        策略：
        1. 写入Redis (主存储)
        2. 更新本地缓存
        3. 发布失效广播 (如果是更新操作)
        """
        if ttl is None:
            ttl = CACHE_TTL_SECONDS

        cache_key = self._generate_cache_key(feature, params)
        now = time.time()

        # 1. 写入Redis
        try:
            set_json(cache_key, data, ttl)
        except Exception:
            pass

        # 2. 更新本地缓存
        self._local_cache[cache_key] = {
            'data': data,
            'expires_at': now + self._local_cache_ttl,
            'hits': 0
        }

    def invalidate(self, feature: str = None, params: Dict = None):
        """
        使缓存失效

        支持：
        - 按feature批量失效
        - 按精确key失效
        - 分布式广播失效
        """
        redis_client = get_redis_client()

        if params:
            # 精确失效
            cache_key = self._generate_cache_key(feature, params)
            self._local_cache.pop(cache_key, None)
            if redis_client:
                try:
                    redis_client.delete(cache_key)
                    # 广播失效消息
                    redis_client.publish(CACHE_INVALIDATION_CHANNEL, cache_key)
                except Exception:
                    pass
        elif feature:
            # 按feature批量失效
            keys_to_remove = [
                k for k in self._local_cache.keys()
                if k.startswith(f"{CACHE_KEY_PREFIX}{feature}:")
            ]
            for k in keys_to_remove:
                del self._local_cache[k]

            if redis_client:
                try:
                    pattern = f"{CACHE_KEY_PREFIX}{feature}:*"
                    cursor = 0
                    while True:
                        cursor, keys = redis_client.scan(cursor, match=pattern, count=100)
                        if keys:
                            redis_client.delete(*keys)
                            for key in keys:
                                redis_client.publish(CACHE_INVALIDATION_CHANNEL, key)
                        if cursor == 0:
                            break
                except Exception:
                    pass
        else:
            # 全部失效
            self._local_cache.clear()

    def get_with_lock(self, feature: str, params: Dict) -> tuple[Optional[Dict], bool]:
        """
        带锁的缓存获取

        返回: (缓存数据, 是否获取到锁)
        - 如果有缓存，返回(数据, False)
        - 如果没缓存且获取到锁，返回(None, True)
        - 如果没缓存且没获取到锁，返回(None, False) - 需要等待
        """
        cache_key = self._generate_cache_key(feature, params)

        # 先尝试获取缓存
        data = self.get(feature, params)
        if data:
            return data, False

        # 尝试获取锁
        if self._acquire_lock(cache_key):
            # 获取锁后再次检查缓存 (双重检查)
            data = self.get(feature, params)
            if data:
                self._release_lock(cache_key)
                return data, False
            return None, True

        return None, False

    def release_lock_after_set(self, feature: str, params: Dict):
        """设置缓存后释放锁"""
        cache_key = self._generate_cache_key(feature, params)
        self._release_lock(cache_key)


# 全局缓存管理器实例
cache_manager = OpenAICacheManager()


# ============== OpenAI 请求处理器 ==============

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


# ============== 路由定义 ==============

@openai_bp.route('/models', methods=['GET'])
@openai_auth_required
def list_models():
    """
    获取可用模型列表

    兼容 OpenAI /v1/models
    """
    try:
        # 获取缓存
        cached = cache_manager.get('models', {'version': 'v1'})
        if cached:
            return jsonify(cached)

        # 构建响应
        response = {
            "object": "list",
            "data": SUPPORTED_MODELS
        }

        # 设置缓存
        cache_manager.set('models', {'version': 'v1'}, response, ttl=3600)  # 1小时缓存

        return jsonify(response)

    except Exception as e:
        return ApiResponse.error(str(e), 500).to_response()


@openai_bp.route('/models/<model_id>', methods=['GET'])
@openai_auth_required
def get_model(model_id: str):
    """
    获取模型详情

    兼容 OpenAI /v1/models/{model}
    """
    try:
        # 查找模型
        model = None
        for m in SUPPORTED_MODELS:
            if m['id'] == model_id:
                model = m
                break

        if not model:
            return ApiResponse.error(f"Model not found: {model_id}", 404).to_response()

        return jsonify(model)

    except Exception as e:
        return ApiResponse.error(str(e), 500).to_response()


@openai_bp.route('/chat/completions', methods=['POST'])
@openai_auth_required
def chat_completions():
    """
    Chat Completions API

    兼容 OpenAI /v1/chat/completions
    支持流式和非流式响应
    """
    handler = OpenAIRequestHandler()
    handler.start_time = time.time()

    try:
        # 1. 获取并验证请求数据
        data = validate_json_request(required_fields=['messages', 'model'])
        if isinstance(data, tuple):
            return data

        # 2. 验证请求
        valid, error_msg = handler.validate_chat_request(data)
        if not valid:
            return ApiResponse.error(error_msg, 400).to_response()

        # 3. 获取用户信息和token
        user = get_current_user()
        token = get_current_token()
        user_id = user.id if user else (token.user_id if token else 0)

        # 4. 提取参数
        model = data.get('model', 'gpt-3.5-turbo')
        messages = data.get('messages', [])
        stream = data.get('stream', False)
        temperature = data.get('temperature', 0.7)
        max_tokens = data.get('max_tokens', 2000)

        # OpenAI 标准额外参数
        top_p = data.get('top_p', 1.0)
        presence_penalty = data.get('presence_penalty', 0)
        frequency_penalty = data.get('frequency_penalty', 0)
        stop = data.get('stop', None)
        n = data.get('n', 1)  # 生成数量，目前只支持 1
        seed = data.get('seed', None)  # 随机种子
        response_format = data.get('response_format', None)  # 响应格式
        tools = data.get('tools', None)  # 工具定义
        tool_choice = data.get('tool_choice', None)  # 工具选择
        user = data.get('user', None)  # 用户标识

        # 5. 检查缓存 (非流式请求)
        cache_params = None
        cache_hit = False
        if not stream:
            cache_params = handler.build_cache_params(data)
            cached_response = cache_manager.get('chat', cache_params)
            if cached_response:
                cache_hit = True
                latency_ms = (time.time() - handler.start_time) * 1000
                handler.log_request(user_id, 'openai_chat', cache_params, latency_ms, True)
                return jsonify(cached_response)

        # 6. 转发到实际的AI服务
        from services.ai_service import call_llm_production

        # 转换消息格式
        llm_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
        ]

        # 系统提示词
        system_prompt = None
        if llm_messages and llm_messages[0].get('role') == 'system':
            system_prompt = llm_messages[0]['content']
            llm_messages = llm_messages[1:]

        # 调用LLM，只传递服务层支持的参数
        llm_kwargs = {}
        if temperature is not None:
            llm_kwargs['temperature'] = temperature
        if max_tokens is not None:
            llm_kwargs['max_tokens'] = max_tokens
        if response_format is not None:
            llm_kwargs['response_format'] = response_format

        result = call_llm_production(
            feature='openai_chat',
            messages=llm_messages,
            user_id=user_id,
            user_email=user.email if user else "",
            use_cache=False,  # 我们自己管理缓存
            **llm_kwargs
        )

        # 7. 处理响应
        if not result['success']:
            error_code = result.get('error_code', 500)
            error_msg = result.get('error', 'Unknown error')
            return ApiResponse.error(error_msg, error_code).to_response()

        content = result.get('data', '')
        usage = result.get('usage', {})

        # 8. 流式响应
        if stream:
            def generate():
                # 模拟流式输出
                chunk_size = 10  # 每个chunk的字符数
                for i in range(0, len(content), chunk_size):
                    chunk = content[i:i+chunk_size]
                    yield handler.create_stream_chunk(chunk, model)
                    time.sleep(0.05)  # 模拟延迟

                # 发送结束标记
                yield handler.create_stream_chunk("", model, "stop")
                yield "data: [DONE]\n\n"

            return Response(
                stream_with_context(generate()),
                mimetype='text/plain',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no'
                }
            )

        # 9. 非流式响应
        response_data = handler.create_response(content, model, usage)

        # 10. 写入缓存
        if cache_params:
            cache_manager.set('chat', cache_params, response_data)

        # 11. 记录日志
        latency_ms = (time.time() - handler.start_time) * 1000
        handler.log_request(user_id, 'openai_chat', cache_params or {}, latency_ms, False)

        return jsonify(response_data)

    except Exception as e:
        return ApiResponse.error(str(e), 500).to_response()


@openai_bp.route('/embeddings', methods=['POST'])
@openai_auth_required
def create_embeddings():
    """
    Embeddings API

    兼容 OpenAI /v1/embeddings
    """
    try:
        data = validate_json_request(required_fields=['input'])
        if isinstance(data, tuple):
            return data

        # 获取用户
        user = get_current_user()
        token = get_current_token()
        user_id = user.id if user else (token.user_id if token else 0)

        # 提取参数
        model = data.get('model', 'text-embedding-ada-002')
        input_data = data.get('input', [])

        # 检查缓存
        cache_params = {'model': model, 'input': input_data}
        cached = cache_manager.get('embedding', cache_params)
        if cached:
            return jsonify(cached)

        # 这里应该调用实际的embedding服务
        # 目前返回模拟数据
        if isinstance(input_data, str):
            input_data = [input_data]

        embeddings = []
        for i, text in enumerate(input_data):
            # 生成模拟的embedding (实际应该调用embedding服务)
            embedding_vector = [0.0] * 1536  # OpenAI embedding维度
            import random
            embedding_vector = [random.gauss(0, 0.1) for _ in range(1536)]

            embeddings.append({
                "object": "embedding",
                "embedding": embedding_vector,
                "index": i
            })

        response = {
            "object": "list",
            "data": embeddings,
            "model": model,
            "usage": {
                "prompt_tokens": sum(len(str(t)) for t in input_data) // 4,
                "total_tokens": sum(len(str(t)) for t in input_data) // 4
            }
        }

        # 写入缓存
        cache_manager.set('embedding', cache_params, response, ttl=3600)  # embedding缓存时间更长

        return jsonify(response)

    except Exception as e:
        return ApiResponse.error(str(e), 500).to_response()


@openai_bp.route('/usage', methods=['GET'])
@openai_auth_required
def get_usage():
    """
    获取使用情况 (扩展API)

    非标准OpenAI API，提供使用统计
    """
    try:
        user = get_current_user()
        if not user:
            return ApiResponse.unauthorized().to_response()

        # 获取日期范围
        from datetime import datetime, timedelta

        days = request.args.get('days', 30, type=int)
        start_date = datetime.utcnow() - timedelta(days=days)

        # 查询使用统计
        from sqlalchemy import func
        from models.ai_request_log import AIRequestLog

        stats = AIRequestLog.query.with_entities(
            AIRequestLog.feature,
            func.count(AIRequestLog.id).label('request_count'),
            func.sum(AIRequestLog.total_tokens).label('total_tokens'),
            func.avg(AIRequestLog.latency_ms).label('avg_latency'),
            func.sum(AIRequestLog.cache_hit.cast(db.Integer)).label('cache_hits')
        ).filter(
            AIRequestLog.user_id == user.id,
            AIRequestLog.created_at >= start_date
        ).group_by(AIRequestLog.feature).all()

        return ApiResponse.success({
            'period': f'{days}d',
            'stats': [
                {
                    'feature': s.feature,
                    'request_count': s.request_count,
                    'total_tokens': s.total_tokens or 0,
                    'avg_latency_ms': round(s.avg_latency, 2) if s.avg_latency else 0,
                    'cache_hit_rate': round(s.cache_hits / s.request_count * 100, 2) if s.request_count else 0
                }
                for s in stats
            ]
        }).to_response()

    except Exception as e:
        return ApiResponse.error(str(e), 500).to_response()


@openai_bp.route('/cache/invalidate', methods=['POST'])
@openai_auth_required
def invalidate_cache():
    """
    缓存失效接口 (管理功能)

    使指定feature或全部缓存失效
    """
    try:
        user = get_current_user()
        if not user:
            return ApiResponse.unauthorized().to_response()

        # 检查权限 (仅管理员)
        is_admin = getattr(user, 'role', None) and user.role.value == 'admin'
        if not is_admin:
            return ApiResponse.forbidden("Admin permission required").to_response()

        data = request.get_json() or {}
        feature = data.get('feature')  # 如 'chat', 'embedding', 'models'

        # 执行缓存失效
        cache_manager.invalidate(feature=feature)

        return ApiResponse.success({
            'feature': feature or 'all',
            'message': 'Cache invalidated successfully'
        }, "Cache invalidated").to_response()

    except Exception as e:
        return ApiResponse.error(str(e), 500).to_response()
