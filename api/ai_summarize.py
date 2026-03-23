"""
AI 智能摘要 API - 生产级别实现

功能：自动为任务/项目生成摘要
特性：
- 支持多种摘要类型
- 支持不同摘要长度
- 支持增量更新
- 支持批量摘要
- 支持摘要版本控制
"""

import bleach
from flask import Blueprint, request
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from models import db, Task, Project, TaskStatus
from core.auth import unified_auth_required, get_current_user
from api.base import ApiResponse, validate_json_request, handle_api_error
from services.ai_service import call_llm_production, AIErrorCode

ai_summarize_bp = Blueprint('ai_summarize', __name__)

# 配置常量
MAX_CONTENT_LENGTH = 10000  # 最大内容长度
MIN_SUMMARY_LENGTH = 50     # 最小摘要长度
MAX_SUMMARY_LENGTH = 1000   # 最大摘要长度
DEFAULT_TASK_SUMMARY_LENGTH = 200
DEFAULT_PROJECT_SUMMARY_LENGTH = 400
CACHE_TTL = 3600  # 1小时

# 提示词模板
TASK_SUMMARY_PROMPT = """你是一个专业的任务摘要专家。请为以下任务生成简洁准确的摘要。

摘要要求：
1. 突出任务的核心目标和关键点
2. 包含当前状态和优先级信息
3. 说明任务的重要性和影响范围
4. 语言简洁专业，不超过{max_length}字
5. 使用第三人称客观描述

请直接返回摘要内容，不要包含任何格式化标记或说明文字。"""

PROJECT_SUMMARY_PROMPT = """你是一个专业的项目摘要专家。请为以下项目生成全面的摘要。

摘要要求：
1. 概述项目目标和当前状态
2. 包含进度统计和完成情况
3. 指出主要进展或潜在风险
4. 说明下一步关键行动
5. 语言专业，不超过{max_length}字

请直接返回摘要内容，不要包含任何格式化标记或说明文字。"""

BATCH_SUMMARY_PROMPT = """你是一个专业的批量摘要专家。请为以下多个条目生成简洁的摘要列表。

要求：
1. 为每个条目生成一句话摘要
2. 摘要要捕捉核心要点
3. 使用一致的格式
4. 每个摘要不超过100字

请返回 JSON 格式：
{
    "summaries": [
        {"id": "id1", "summary": "摘要1"},
        {"id": "id2", "summary": "摘要2"}
    ]
}"""


def sanitize_content(text: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
    """清理内容"""
    if not text:
        return ""
    text = text[:max_length]
    text = bleach.clean(text, tags=[], strip=True)
    return text.strip()


def truncate_content(text: str, max_chars: int = 3000) -> str:
    """截断内容，保留关键信息"""
    if not text or len(text) <= max_chars:
        return text

    # 尝试在段落边界截断
    truncated = text[:max_chars]
    last_para = truncated.rfind('\n\n')
    last_sent = truncated.rfind('. ')

    if last_para > max_chars * 0.8:
        return truncated[:last_para] + "\n\n..."
    elif last_sent > max_chars * 0.8:
        return truncated[:last_sent + 1] + " ..."
    else:
        return truncated + " ..."


def get_task_stats(task_id: int) -> Dict[str, Any]:
    """获取任务统计信息"""
    task = Task.query.get(task_id)
    if not task:
        return {}

    return {
        'title': task.title,
        'status': task.status.value if task.status else 'unknown',
        'priority': task.priority.value if task.priority else 'unknown',
        'created_at': task.created_at.isoformat() if task.created_at else None,
        'updated_at': task.updated_at.isoformat() if task.updated_at else None,
        'has_description': bool(task.content),
        'content_length': len(task.content) if task.content else 0
    }


def get_project_stats(project_id: int) -> Dict[str, Any]:
    """获取项目统计信息"""
    project = Project.query.get(project_id)
    if not project:
        return {}

    # 任务统计
    total_tasks = Task.query.filter_by(project_id=project_id).count()
    completed_tasks = Task.query.filter_by(
        project_id=project_id, status=TaskStatus.DONE
    ).count()
    in_progress_tasks = Task.query.filter_by(
        project_id=project_id, status=TaskStatus.IN_PROGRESS
    ).count()
    todo_tasks = Task.query.filter_by(
        project_id=project_id, status=TaskStatus.TODO
    ).count()

    completion_rate = round(
        completed_tasks / total_tasks * 100, 1
    ) if total_tasks > 0 else 0

    return {
        'name': project.name,
        'description': project.description,
        'status': project.status.value if project.status else 'unknown',
        'total_tasks': total_tasks,
        'completed': completed_tasks,
        'in_progress': in_progress_tasks,
        'todo': todo_tasks,
        'completion_rate': completion_rate,
        'created_at': project.created_at.isoformat() if project.created_at else None,
        'last_activity_at': project.last_activity_at.isoformat() if project.last_activity_at else None
    }


@ai_summarize_bp.route('/ai/summarize', methods=['POST'])
@unified_auth_required
def summarize():
    """
    AI 智能摘要 - 为任务或项目生成摘要

    Request Body:
        {
            "type": "task" | "project",
            "id": 123,
            "max_length": 200,        // 摘要最大长度
            "use_cache": true,
            "force_refresh": false    // 强制刷新缓存
        }

    Response:
        {
            "code": 200,
            "data": {
                "type": "task",
                "id": 123,
                "summary": "...",
                "max_length": 200,
                "generated_at": "...",
                "stats": {...}
            }
        }
    """
    try:
        # 1. 验证请求数据
        data = validate_json_request(required_fields=['type', 'id'])
        if isinstance(data, tuple):
            return data

        content_type = data.get('type')
        content_id = data.get('id')
        max_length = data.get('max_length')
        use_cache = data.get('use_cache', True)
        force_refresh = data.get('force_refresh', False)

        # 2. 验证类型
        if content_type not in ['task', 'project']:
            return ApiResponse.error(
                'Type must be "task" or "project"',
                400
            ).to_response()

        # 3. 获取当前用户
        user = get_current_user()
        user_id = user.id if user else 0
        user_email = user.email if user else ""

        # 4. 设置默认长度
        if max_length is None:
            max_length = DEFAULT_TASK_SUMMARY_LENGTH if content_type == 'task' else DEFAULT_PROJECT_SUMMARY_LENGTH

        max_length = max(MIN_SUMMARY_LENGTH, min(max_length, MAX_SUMMARY_LENGTH))

        # 5. 生成摘要
        if content_type == 'task':
            result = _summarize_task(
                content_id, max_length, user, use_cache, force_refresh
            )
        else:
            result = _summarize_project(
                content_id, max_length, user, use_cache, force_refresh
            )

        # 6. 处理错误
        if not result['success']:
            return ApiResponse.error(result['error'], 400).to_response()

        return ApiResponse.success({
            'type': content_type,
            'id': content_id,
            'summary': result['summary'],
            'max_length': max_length,
            'generated_at': datetime.utcnow().isoformat(),
            'stats': result.get('stats', {}),
            'ai_metadata': result.get('ai_metadata', {})
        }, "Summary generated successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


def _summarize_task(task_id: int, max_length: int, user, use_cache: bool,
                    force_refresh: bool) -> Dict[str, Any]:
    """生成任务摘要"""
    task = Task.query.get(task_id)
    if not task:
        return {'success': False, 'error': 'Task not found'}

    user_id = user.id if user else 0
    user_email = user.email if user else ""

    # 检查缓存
    if use_cache and not force_refresh:
        # TODO: 从数据库或缓存获取已有摘要
        pass

    # 准备内容
    content_parts = [f"任务标题：{sanitize_content(task.title, 200)}"]

    if task.content:
        content = sanitize_content(task.content, MAX_CONTENT_LENGTH)
        content = truncate_content(content, 3000)
        content_parts.append(f"任务描述：{content}")

    if task.status:
        content_parts.append(f"当前状态：{task.status.value}")
    if task.priority:
        content_parts.append(f"优先级：{task.priority.value}")

    full_content = "\n\n".join(content_parts)

    # 构建提示词
    messages = [
        {"role": "system", "content": TASK_SUMMARY_PROMPT.format(max_length=max_length)},
        {"role": "user", "content": full_content}
    ]

    # 调用 LLM
    result = call_llm_production(
        feature='summarize_task',
        messages=messages,
        user_id=user_id,
        user_email=user_email,
        use_cache=use_cache and not force_refresh,
        cache_params={
            'task_id': task_id,
            'max_length': max_length,
            'content_hash': hash(task.content) if task.content else 0
        },
        temperature=0.5,
        max_tokens=min(max_length, 500)
    )

    if not result['success']:
        return result

    summary = sanitize_content(result['data'], max_length)

    return {
        'success': True,
        'summary': summary,
        'stats': get_task_stats(task_id),
        'ai_metadata': {
            'request_id': result.get('context', {}).get('request_id'),
            'cached': result.get('cached', False),
            'tokens_used': result.get('usage', {}).get('total_tokens', 0)
        }
    }


def _summarize_project(project_id: int, max_length: int, user, use_cache: bool,
                       force_refresh: bool) -> Dict[str, Any]:
    """生成项目摘要"""
    project = Project.query.get(project_id)
    if not project:
        return {'success': False, 'error': 'Project not found'}

    user_id = user.id if user else 0
    user_email = user.email if user else ""

    # 准备内容
    stats = get_project_stats(project_id)

    content_parts = [f"项目名称：{sanitize_content(project.name, 200)}"]

    if project.description:
        desc = sanitize_content(project.description, MAX_CONTENT_LENGTH)
        desc = truncate_content(desc, 2000)
        content_parts.append(f"项目描述：{desc}")

    content_parts.extend([
        f"任务统计：",
        f"- 总任务数：{stats['total_tasks']}",
        f"- 已完成：{stats['completed']} ({stats['completion_rate']}%)",
        f"- 进行中：{stats['in_progress']}",
        f"- 待办：{stats['todo']}",
        f"项目状态：{stats['status']}",
    ])

    # 获取最近完成的任务
    recent_completed = Task.query.filter_by(
        project_id=project_id,
        status=TaskStatus.DONE
    ).order_by(Task.updated_at.desc()).limit(5).all()

    if recent_completed:
        content_parts.append("最近完成的任务：")
        for task in recent_completed:
            content_parts.append(f"- {sanitize_content(task.title, 100)}")

    # 获取进行中的任务
    in_progress = Task.query.filter_by(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS
    ).order_by(Task.priority.desc()).limit(5).all()

    if in_progress:
        content_parts.append("进行中的任务：")
        for task in in_progress:
            content_parts.append(
                f"- {sanitize_content(task.title, 100)} "
                f"({task.priority.value if task.priority else 'unknown'})"
            )

    full_content = "\n".join(content_parts)

    # 构建提示词
    messages = [
        {"role": "system", "content": PROJECT_SUMMARY_PROMPT.format(max_length=max_length)},
        {"role": "user", "content": full_content}
    ]

    # 调用 LLM
    result = call_llm_production(
        feature='summarize_project',
        messages=messages,
        user_id=user_id,
        user_email=user_email,
        use_cache=use_cache and not force_refresh,
        cache_params={
            'project_id': project_id,
            'max_length': max_length,
            'stats_hash': hash(str(stats))
        },
        temperature=0.5,
        max_tokens=min(max_length, 800)
    )

    if not result['success']:
        return result

    summary = sanitize_content(result['data'], max_length)

    return {
        'success': True,
        'summary': summary,
        'stats': stats,
        'ai_metadata': {
            'request_id': result.get('context', {}).get('request_id'),
            'cached': result.get('cached', False),
            'tokens_used': result.get('usage', {}).get('total_tokens', 0)
        }
    }


@ai_summarize_bp.route('/ai/summarize/batch', methods=['POST'])
@unified_auth_required
def batch_summarize():
    """
    批量生成摘要

    Request Body:
        {
            "type": "task" | "project",
            "ids": [1, 2, 3],
            "max_length": 100,
            "use_cache": true
        }
    """
    try:
        data = validate_json_request(required_fields=['type', 'ids'])
        if isinstance(data, tuple):
            return data

        content_type = data.get('type')
        ids = data.get('ids', [])
        max_length = data.get('max_length', 100)
        use_cache = data.get('use_cache', True)

        # 验证参数
        if not isinstance(ids, list):
            return ApiResponse.error('ids must be a list', 400).to_response()

        if len(ids) == 0:
            return ApiResponse.error('ids cannot be empty', 400).to_response()

        if len(ids) > 20:
            return ApiResponse.error('Maximum 20 items allowed', 400).to_response()

        user = get_current_user()
        max_length = max(50, min(max_length, 200))

        # 批量生成摘要
        summaries = []
        for item_id in ids:
            if content_type == 'task':
                result = _summarize_task(item_id, max_length, user, use_cache, False)
            else:
                result = _summarize_project(item_id, max_length, user, use_cache, False)

            summaries.append({
                'id': item_id,
                'success': result['success'],
                'summary': result.get('summary') if result['success'] else None,
                'error': result.get('error') if not result['success'] else None
            })

        return ApiResponse.success({
            'type': content_type,
            'summaries': summaries,
            'total': len(summaries),
            'successful': sum(1 for s in summaries if s['success']),
            'failed': sum(1 for s in summaries if not s['success'])
        }, "Batch summarization completed").to_response()

    except Exception as e:
        return handle_api_error(e)


@ai_summarize_bp.route('/ai/summarize/history', methods=['GET'])
@unified_auth_required
def get_summary_history():
    """
    获取摘要生成历史

    Query Parameters:
        type: task | project
        id: 对象ID
        limit: 返回数量（默认10）
    """
    try:
        content_type = request.args.get('type')
        content_id = request.args.get('id', type=int)
        limit = request.args.get('limit', 10, type=int)

        if not content_type or not content_id:
            return ApiResponse.error(
                'type and id are required',
                400
            ).to_response()

        # TODO: 从数据库查询历史摘要
        # 目前返回空列表，后续可以实现摘要版本存储

        return ApiResponse.success({
            'type': content_type,
            'id': content_id,
            'history': [],
            'has_history': False
        }).to_response()

    except Exception as e:
        return handle_api_error(e)


@ai_summarize_bp.route('/ai/summarize/stats', methods=['GET'])
@unified_auth_required
def get_summarize_stats():
    """
    获取摘要功能使用统计
    """
    try:
        user = get_current_user()
        if not user:
            return ApiResponse.error('Authentication required', 401).to_response()

        days = request.args.get('days', 30, type=int)

        # 限制查询范围
        days = min(max(days, 1), 90)

        from models.ai_request_log import AIRequestLog
        from datetime import datetime, timedelta

        start_date = datetime.utcnow() - timedelta(days=days)

        # 获取用户的摘要使用统计
        stats = AIRequestLog.get_stats_by_user(user.id, days=days)

        # 只统计 summarize 相关的功能
        summarize_features = AIRequestLog.query.filter(
            AIRequestLog.user_id == user.id,
            AIRequestLog.feature.like('summarize%'),
            AIRequestLog.created_at >= start_date
        ).all()

        feature_breakdown = {}
        for log in summarize_features:
            if log.feature not in feature_breakdown:
                feature_breakdown[log.feature] = {
                    'count': 0,
                    'tokens': 0
                }
            feature_breakdown[log.feature]['count'] += 1
            feature_breakdown[log.feature]['tokens'] += log.total_tokens

        return ApiResponse.success({
            'period_days': days,
            'total_requests': stats.request_count or 0,
            'total_tokens': stats.total_tokens or 0,
            'avg_latency_ms': round(stats.avg_latency, 2) if stats.avg_latency else 0,
            'feature_breakdown': feature_breakdown
        }).to_response()

    except Exception as e:
        return handle_api_error(e)
