"""
AI 任务助手 API - 生产级别实现

功能：根据简单描述自动生成任务信息
特性：
- 输入验证和 sanitization
- 限流和缓存
- 审计日志
- 优雅错误处理
- 流式响应支持
"""

import re
import bleach
from flask import Blueprint, request, Response, stream_with_context
from models import db
from core.auth import unified_auth_required, get_current_user
from api.base import ApiResponse, validate_json_request, handle_api_error
from services.ai_service import call_llm_production, AIErrorCode

ai_task_assistant_bp = Blueprint('ai_task_assistant', __name__)

# 配置常量
MAX_DESCRIPTION_LENGTH = 2000
MAX_CONTEXT_LENGTH = 1000
CACHE_TTL_SECONDS = 3600  # 1小时缓存

# 提示词模板
TASK_ASSISTANT_SYSTEM_PROMPT = """你是一个专业的任务管理助手。根据用户提供的描述，生成结构化的任务信息。

请严格遵守以下规则：
1. 标题必须简洁明了，不超过50个字符
2. 描述必须详细，包含：背景、目标、具体步骤、验收标准
3. 优先级必须是以下之一：low, medium, high, urgent
4. 标签必须是与任务相关的关键词，最多5个
5. 预估时间必须是合理的数字（小时）

返回格式必须是有效的 JSON：
{
    "title": "任务标题",
    "description": "详细描述",
    "priority": "medium",
    "tags": ["标签1", "标签2"],
    "estimated_hours": 8
}"""

TASK_ENHANCE_SYSTEM_PROMPT = """你是一个专业的任务描述优化专家。请优化任务描述，使其更详细、更专业。

优化要点：
1. 标题要清晰、具体、可执行
2. 描述要包含：背景、目标、具体步骤、验收标准、风险点
3. 提供3-5个实用的完成建议
4. 列出关键检查项（checklist）
5. 识别潜在风险并提供规避建议

返回格式必须是有效的 JSON：
{
    "title": "优化后的标题",
    "description": "优化后的详细描述",
    "suggestions": ["建议1", "建议2"],
    "checklist": ["检查项1", "检查项2"],
    "risks": ["风险1", "风险2"],
    "mitigations": ["规避措施1", "规避措施2"]
}"""


def sanitize_input(text: str, max_length: int = 2000) -> str:
    """
    清理用户输入，防止 XSS 和注入攻击
    """
    if not text:
        return ""

    # 截断超长文本
    text = text[:max_length]

    # 使用 bleach 清理 HTML 标签
    text = bleach.clean(text, tags=[], strip=True)

    # 移除控制字符
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)

    # 规范化空白字符
    text = re.sub(r'\s+', ' ', text)

    return text.strip()


def validate_task_data(data: dict) -> tuple[bool, str]:
    """
    验证 AI 生成的任务数据
    """
    # 检查必填字段
    if not data.get('title'):
        return False, "Title is required"

    if not data.get('description'):
        return False, "Description is required"

    # 验证标题长度
    title = data.get('title', '')
    if len(title) > 200:
        data['title'] = title[:200]

    # 验证优先级
    valid_priorities = ['low', 'medium', 'high', 'urgent']
    priority = data.get('priority', 'medium').lower()
    if priority not in valid_priorities:
        priority = 'medium'
    data['priority'] = priority

    # 验证标签
    tags = data.get('tags', [])
    if not isinstance(tags, list):
        tags = []
    # 最多5个标签，每个标签最多20字符
    tags = [str(t)[:20] for t in tags[:5]]
    data['tags'] = tags

    # 验证预估时间
    estimated = data.get('estimated_hours')
    if estimated is not None:
        try:
            estimated = float(estimated)
            if estimated < 0 or estimated > 1000:
                estimated = None
            else:
                estimated = round(estimated, 1)
        except (ValueError, TypeError):
            estimated = None
        data['estimated_hours'] = estimated

    return True, ""


def parse_llm_json_response(content: str) -> dict:
    """
    解析 LLM 返回的 JSON 响应
    支持多种格式：直接 JSON、markdown 代码块、带注释的 JSON
    """
    import json

    if not content:
        return {'success': False, 'error': 'Empty response'}

    # 尝试直接解析
    try:
        return {'success': True, 'data': json.loads(content.strip())}
    except json.JSONDecodeError:
        pass

    # 尝试提取 markdown 代码块
    import re

    # 匹配 ```json ... ``` 或 ``` ... ```
    patterns = [
        r'```json\s*(.*?)\s*```',
        r'```\s*(.*?)\s*```',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, content, re.DOTALL)
        if matches:
            try:
                return {'success': True, 'data': json.loads(matches[0].strip())}
            except json.JSONDecodeError:
                continue

    # 尝试提取 { ... } 部分
    try:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return {'success': True, 'data': json.loads(match.group())}
    except json.JSONDecodeError:
        pass

    return {'success': False, 'error': 'Failed to parse JSON', 'raw': content}


@ai_task_assistant_bp.route('/ai/task-assistant', methods=['POST'])
@unified_auth_required
def task_assistant():
    """
    AI 任务助手 - 根据描述生成任务信息

    Request Body:
        {
            "description": "任务简单描述",
            "project_context": "项目背景（可选）",
            "use_cache": true,  // 是否使用缓存
            "stream": false     // 是否流式返回
        }

    Response:
        {
            "code": 200,
            "data": {
                "title": "任务标题",
                "description": "详细描述",
                "priority": "medium",
                "tags": ["标签1"],
                "estimated_hours": 8
            }
        }
    """
    try:
        # 1. 获取并验证请求数据
        data = validate_json_request(
            required_fields=['description'],
            optional_fields=['project_context', 'stream', 'use_cache'],
        )
        if isinstance(data, tuple):
            return data

        # 2. 获取当前用户
        user = get_current_user()
        user_id = user.id if user else 0
        user_email = user.email if user else ""

        # 3. 清理和验证输入
        description = sanitize_input(data.get('description', ''), MAX_DESCRIPTION_LENGTH)
        if not description:
            return ApiResponse.error('Description is required', 400).to_response()

        project_context = sanitize_input(data.get('project_context', ''), MAX_CONTEXT_LENGTH)
        use_cache = data.get('use_cache', True)
        stream = data.get('stream', False)

        # 4. 构建提示词
        user_prompt = f"请为以下任务生成详细信息：\n\n用户描述：{description}"
        if project_context:
            user_prompt += f"\n\n项目背景：{project_context}"

        messages = [
            {"role": "system", "content": TASK_ASSISTANT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]

        # 5. 构建缓存参数
        cache_params = None
        if use_cache:
            cache_params = {
                'description': description,
                'context': project_context,
                'version': 'v1'
            }

        # 6. 调用 LLM
        result = call_llm_production(
            feature='task_assistant',
            messages=messages,
            user_id=user_id,
            user_email=user_email,
            use_cache=use_cache,
            cache_params=cache_params,
            temperature=0.7,
            max_tokens=1500
        )

        # 7. 处理错误
        if not result['success']:
            error_code = result.get('error_code', AIErrorCode.UNKNOWN_ERROR.value)
            error_msg = result.get('error', 'Unknown error')

            if error_code == AIErrorCode.RATE_LIMIT_EXCEEDED.value:
                return ApiResponse.error('Rate limit exceeded, please try again later', 429).to_response()
            elif error_code == AIErrorCode.TIMEOUT.value:
                return ApiResponse.error('Request timeout, please try again', 504).to_response()
            else:
                return ApiResponse.error(f'AI service error: {error_msg}', 500).to_response()

        # 8. 解析 JSON 响应
        parsed = parse_llm_json_response(result['data'])
        if not parsed['success']:
            return ApiResponse.error(
                f'Failed to parse AI response: {parsed.get("error")}',
                500
            ).to_response()

        task_data = parsed['data']

        # 9. 验证生成的数据
        valid, error_msg = validate_task_data(task_data)
        if not valid:
            return ApiResponse.error(f'Invalid AI response: {error_msg}', 500).to_response()

        # 10. 构建响应
        response_data = {
            'title': task_data.get('title', ''),
            'description': task_data.get('description', ''),
            'priority': task_data.get('priority', 'medium'),
            'tags': task_data.get('tags', []),
            'estimated_hours': task_data.get('estimated_hours'),
            'original_description': description,
            'ai_metadata': {
                'request_id': result.get('context', {}).get('request_id'),
                'cached': result.get('cached', False),
                'model': result.get('model'),
                'tokens_used': result.get('usage', {}).get('total_tokens', 0)
            }
        }

        return ApiResponse.success(
            response_data,
            "Task generated successfully by AI"
        ).to_response()

    except Exception as e:
        return handle_api_error(e)


@ai_task_assistant_bp.route('/ai/task-assistant/enhance', methods=['POST'])
@unified_auth_required
def enhance_task():
    """
    AI 任务增强 - 优化现有任务描述

    Request Body:
        {
            "title": "当前任务标题",
            "description": "当前任务描述",
            "use_cache": true
        }
    """
    try:
        # 1. 获取并验证请求数据
        data = validate_json_request(
            required_fields=['title'],
            optional_fields=['description', 'use_cache'],
        )
        if isinstance(data, tuple):
            return data

        # 2. 获取当前用户
        user = get_current_user()
        user_id = user.id if user else 0
        user_email = user.email if user else ""

        # 3. 清理输入
        title = sanitize_input(data.get('title', ''), 200)
        description = sanitize_input(data.get('description', ''), MAX_DESCRIPTION_LENGTH)
        use_cache = data.get('use_cache', True)

        if not title:
            return ApiResponse.error('Title is required', 400).to_response()

        # 4. 构建提示词
        user_prompt = f"请优化以下任务：\n\n标题：{title}"
        if description:
            user_prompt += f"\n描述：{description}"

        messages = [
            {"role": "system", "content": TASK_ENHANCE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]

        # 5. 构建缓存参数
        cache_params = None
        if use_cache:
            cache_params = {
                'title': title,
                'description': description,
                'type': 'enhance',
                'version': 'v1'
            }

        # 6. 调用 LLM
        result = call_llm_production(
            feature='task_enhance',
            messages=messages,
            user_id=user_id,
            user_email=user_email,
            use_cache=use_cache,
            cache_params=cache_params,
            temperature=0.7,
            max_tokens=2000
        )

        # 7. 处理错误
        if not result['success']:
            return ApiResponse.error(
                f"AI enhancement failed: {result.get('error')}",
                500
            ).to_response()

        # 8. 解析响应
        parsed = parse_llm_json_response(result['data'])
        if not parsed['success']:
            return ApiResponse.error(
                f"Failed to parse response: {parsed.get('error')}",
                500
            ).to_response()

        enhanced_data = parsed['data']

        # 9. 验证并清理数据
        if enhanced_data.get('title'):
            enhanced_data['title'] = sanitize_input(enhanced_data['title'], 200)
        if enhanced_data.get('description'):
            enhanced_data['description'] = sanitize_input(enhanced_data['description'], MAX_DESCRIPTION_LENGTH)

        # 确保列表字段是列表
        for field in ['suggestions', 'checklist', 'risks', 'mitigations']:
            if field in enhanced_data and not isinstance(enhanced_data[field], list):
                enhanced_data[field] = []

        # 10. 构建响应
        response_data = {
            **enhanced_data,
            'ai_metadata': {
                'request_id': result.get('context', {}).get('request_id'),
                'cached': result.get('cached', False),
                'tokens_used': result.get('usage', {}).get('total_tokens', 0)
            }
        }

        return ApiResponse.success(
            response_data,
            "Task enhanced successfully"
        ).to_response()

    except Exception as e:
        return handle_api_error(e)


@ai_task_assistant_bp.route('/ai/task-assistant/stats', methods=['GET'])
@unified_auth_required
def get_stats():
    """
    获取 AI 任务助手使用统计
    """
    try:
        user = get_current_user()
        if not user:
            return ApiResponse.error('Authentication required', 401).to_response()

        # 检查权限（仅管理员可以查看全局统计）
        is_admin = getattr(user, 'role', None) and user.role.value == 'admin'

        if is_admin:
            # 全局统计
            from models.ai_request_log import AIRequestLog
            from datetime import datetime, timedelta

            start_date = datetime.utcnow() - timedelta(days=30)

            stats = AIRequestLog.get_stats_by_feature(start_date=start_date)

            return ApiResponse.success({
                'period': '30d',
                'stats': [
                    {
                        'feature': s.feature,
                        'request_count': s.request_count,
                        'total_tokens': s.total_tokens,
                        'avg_latency_ms': round(s.avg_latency, 2) if s.avg_latency else 0,
                        'cache_hit_rate': round(s.cache_hits / s.request_count * 100, 2) if s.request_count else 0
                    }
                    for s in stats
                ]
            }).to_response()
        else:
            # 用户个人统计
            from models.ai_request_log import AIRequestLog

            stats = AIRequestLog.get_stats_by_user(user.id, days=30)

            return ApiResponse.success({
                'period': '30d',
                'user_id': user.id,
                'request_count': stats.request_count or 0,
                'total_tokens': stats.total_tokens or 0,
                'avg_latency_ms': round(stats.avg_latency, 2) if stats.avg_latency else 0
            }).to_response()

    except Exception as e:
        return handle_api_error(e)
