"""OpenAI 兼容端点：models / chat/completions / embeddings / usage / cache invalidate。

get_current_user 经包命名空间运行时解析（_pkg.get_current_user），
保证 patch("api.openai_compatible.get_current_user") 语义不变。
"""

import time

from flask import Blueprint, request, Response, stream_with_context, jsonify
from flask import g

from api.base import ApiResponse, validate_json_request
from core.auth import get_current_token
from models import db
from api import openai_compatible as _pkg
from api.openai_compatible._core import (
    openai_bp,
    openai_auth_required,
)
from api.openai_compatible import cache_manager  # 包级共享实例（测试在实例上打桩）
from api.openai_compatible.handler import OpenAIRequestHandler


def get_current_user():
    """运行时转发到包属性：测试经 patch("api.openai_compatible.get_current_user") 替换。"""
    return _pkg.get_current_user()


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
            "data": _pkg.SUPPORTED_MODELS
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
        for m in _pkg.SUPPORTED_MODELS:
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
        # （optional_fields 必须列全，否则过滤会丢弃 stream/temperature 等
        #   客户端参数——历史版本此处缺省导致流式模式从未生效）
        data = validate_json_request(
            required_fields=['messages', 'model'],
            optional_fields=[
                'temperature', 'max_tokens', 'stream', 'top_p',
                'presence_penalty', 'frequency_penalty', 'stop', 'n',
                'seed', 'response_format', 'tools', 'tool_choice', 'user',
            ],
        )
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
        data = validate_json_request(
            required_fields=['input'],
            optional_fields=['model'],
        )
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
