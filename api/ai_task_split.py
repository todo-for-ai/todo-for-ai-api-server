"""
AI 任务拆分 API - 生产级别实现

功能：将大任务自动拆分为子任务
特性：
- 事务管理（原子性操作）
- 批量创建子任务
- 父子任务关联
- 进度通知
- 回滚机制
"""

import bleach
from flask import Blueprint, request
from sqlalchemy.exc import SQLAlchemyError
from models import db, Task, TaskStatus, TaskPriority
from core.auth import unified_auth_required, get_current_user
from api.base import ApiResponse, validate_json_request, handle_api_error
from services.ai_service import call_llm_production, AIErrorCode

ai_task_split_bp = Blueprint('ai_task_split', __name__)

# 配置常量
MAX_SUBTASKS = 20
MIN_SUBTASKS = 2
DEFAULT_SUBTASKS = 3
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 5000

# 提示词模板
TASK_SPLIT_SYSTEM_PROMPT = """你是一个专业的任务拆分专家。请将大任务拆分为可执行的子任务。

拆分原则：
1. 每个子任务应该是独立的、可完成的单元
2. 子任务之间边界清晰，尽量减少依赖
3. 按照执行顺序排列，标注依赖关系
4. 优先级根据紧急程度和依赖关系确定
5. 子任务标题要简洁明了（不超过50字）
6. 描述要包含具体的完成标准

返回格式必须是有效的 JSON：
{
    "subtasks": [
        {
            "title": "子任务标题",
            "description": "详细描述",
            "priority": "medium",
            "estimated_hours": 4,
            "depends_on": []  // 依赖的子任务序号（从1开始）
        }
    ],
    "execution_order": "并行/顺序/混合执行建议",
    "dependencies": "任务间的依赖关系说明",
    "estimated_total_hours": 总预估时间
}"""


def sanitize_input(text: str, max_length: int = 2000) -> str:
    """清理输入文本"""
    if not text:
        return ""
    text = text[:max_length]
    text = bleach.clean(text, tags=[], strip=True)
    return text.strip()


def validate_subtask_data(subtask: dict, index: int) -> tuple[bool, str]:
    """验证子任务数据"""
    if not subtask.get('title'):
        return False, f"Subtask {index}: Title is required"

    # 验证标题长度
    title = subtask.get('title', '')
    if len(title) > MAX_TITLE_LENGTH:
        subtask['title'] = title[:MAX_TITLE_LENGTH]

    # 验证优先级
    valid_priorities = ['low', 'medium', 'high', 'urgent']
    priority = subtask.get('priority', 'medium').lower()
    if priority not in valid_priorities:
        priority = 'medium'
    subtask['priority'] = priority

    # 验证预估时间
    estimated = subtask.get('estimated_hours')
    if estimated is not None:
        try:
            estimated = float(estimated)
            if estimated < 0 or estimated > 1000:
                estimated = None
            else:
                estimated = round(estimated, 1)
        except (ValueError, TypeError):
            estimated = None
        subtask['estimated_hours'] = estimated

    # 验证依赖关系
    depends_on = subtask.get('depends_on', [])
    if not isinstance(depends_on, list):
        subtask['depends_on'] = []
    else:
        # 确保依赖是整数
        subtask['depends_on'] = [
            int(d) for d in depends_on
            if isinstance(d, (int, str)) and str(d).isdigit()
        ]

    return True, ""


def parse_llm_json_response(content: str) -> dict:
    """解析 LLM 返回的 JSON"""
    import json
    import re

    if not content:
        return {'success': False, 'error': 'Empty response'}

    # 尝试直接解析
    try:
        return {'success': True, 'data': json.loads(content.strip())}
    except json.JSONDecodeError:
        pass

    # 尝试提取 markdown 代码块
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

    # 尝试提取 { ... }
    try:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if match:
            return {'success': True, 'data': json.loads(match.group())}
    except json.JSONDecodeError:
        pass

    return {'success': False, 'error': 'Failed to parse JSON', 'raw': content}


@ai_task_split_bp.route('/tasks/<int:task_id>/ai-split', methods=['POST'])
@unified_auth_required
def split_task(task_id):
    """
    AI 任务拆分 - 将大任务拆分为子任务

    Request Body:
        {
            "num_subtasks": 3,        // 子任务数量（2-20，默认3）
            "use_cache": true,        // 是否使用缓存
            "atomic": true            // 是否原子操作（全部成功或全部失败）
        }

    Response:
        {
            "code": 200,
            "data": {
                "parent_task_id": 123,
                "subtasks": [...],
                "execution_order": "...",
                "dependencies": "...",
                "total_subtasks": 3,
                "estimated_total_hours": 12
            }
        }
    """
    try:
        # 1. 获取父任务
        parent_task = Task.query.get(task_id)
        if not parent_task:
            return ApiResponse.not_found('Parent task not found').to_response()

        # 2. 权限检查
        user = get_current_user()
        if not user:
            return ApiResponse.error('Authentication required', 401).to_response()

        user_id = user.id
        user_email = user.email

        # 3. 获取请求参数
        data = request.get_json() or {}
        num_subtasks = data.get('num_subtasks', DEFAULT_SUBTASKS)
        use_cache = data.get('use_cache', True)

        # 4. 验证参数
        if not isinstance(num_subtasks, int):
            return ApiResponse.error('num_subtasks must be an integer', 400).to_response()

        num_subtasks = max(MIN_SUBTASKS, min(num_subtasks, MAX_SUBTASKS))

        # 5. 清理输入
        title = sanitize_input(parent_task.title, MAX_TITLE_LENGTH)
        description = sanitize_input(parent_task.content or '', MAX_DESCRIPTION_LENGTH)

        # 6. 构建提示词
        user_prompt = f"""请将以下任务拆分为{num_subtasks}个子任务：

任务标题：{title}
任务描述：{description}
优先级：{parent_task.priority.value if parent_task.priority else 'medium'}

请确保子任务覆盖原任务的所有要点，并按照执行顺序排列。"""

        messages = [
            {"role": "system", "content": TASK_SPLIT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]

        # 7. 构建缓存参数
        cache_params = None
        if use_cache:
            cache_params = {
                'task_id': task_id,
                'title': title,
                'num_subtasks': num_subtasks,
                'version': 'v1'
            }

        # 8. 调用 LLM
        result = call_llm_production(
            feature='task_split',
            messages=messages,
            user_id=user_id,
            user_email=user_email,
            use_cache=use_cache,
            cache_params=cache_params,
            temperature=0.7,
            max_tokens=2500
        )

        # 9. 处理错误
        if not result['success']:
            error_code = result.get('error_code', AIErrorCode.UNKNOWN_ERROR.value)
            if error_code == AIErrorCode.RATE_LIMIT_EXCEEDED.value:
                return ApiResponse.error('Rate limit exceeded', 429).to_response()
            return ApiResponse.error(f"AI split failed: {result.get('error')}", 500).to_response()

        # 10. 解析响应
        parsed = parse_llm_json_response(result['data'])
        if not parsed['success']:
            return ApiResponse.error(f"Parse failed: {parsed.get('error')}", 500).to_response()

        split_data = parsed['data']
        subtasks_data = split_data.get('subtasks', [])

        if not subtasks_data:
            return ApiResponse.error('AI failed to generate subtasks', 500).to_response()

        # 11. 验证子任务数据
        validated_subtasks = []
        for i, subtask in enumerate(subtasks_data):
            valid, error_msg = validate_subtask_data(subtask, i + 1)
            if not valid:
                return ApiResponse.error(error_msg, 500).to_response()
            validated_subtasks.append(subtask)

        # 12. 创建子任务（事务）
        created_subtasks = []

        try:
            # 整个请求本就运行在单一事务内（进入时已查询父任务，
            # Session.begin() 在活动事务上会直接 InvalidRequestError），
            # 失败路径统一 rollback，天然具备全有或全无语义
            created_subtasks = _create_subtasks(
                parent_task, validated_subtasks, user_id
            )

            # 13. 更新父任务状态
            if not parent_task.tags:
                parent_task.tags = []

            # 移除旧的 has_subtasks 标签
            parent_task.tags = [
                t for t in parent_task.tags
                if not t.startswith('has_subtasks:')
            ]
            parent_task.tags.append(f"has_subtasks:{len(created_subtasks)}")
            parent_task.tags.append("ai_split:true")

            db.session.commit()

            # Auto-assign AI subtasks
            from services.agent_runtime_controller import AgentRuntimeController
            for subtask_info in created_subtasks:
                subtask = Task.query.get(subtask_info['id'])
                if subtask and subtask.is_ai_task:
                    AgentRuntimeController.auto_assign_task(subtask)

        except SQLAlchemyError as e:
            db.session.rollback()
            return ApiResponse.error(
                f"Database error: {str(e)}",
                500
            ).to_response()

        # 14. 构建响应
        return ApiResponse.success({
            'parent_task_id': task_id,
            'parent_task_title': parent_task.title,
            'subtasks': created_subtasks,
            'execution_order': split_data.get('execution_order', ''),
            'dependencies': split_data.get('dependencies', ''),
            'estimated_total_hours': split_data.get('estimated_total_hours'),
            'total_subtasks': len(created_subtasks),
            'ai_metadata': {
                'request_id': result.get('context', {}).get('request_id'),
                'cached': result.get('cached', False),
                'tokens_used': result.get('usage', {}).get('total_tokens', 0)
            }
        }, "Task split successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e)


def _create_subtasks(parent_task: Task, subtasks_data: list, creator_id: int) -> list:
    """
    创建子任务
    """
    created = []

    for i, subtask_data in enumerate(subtasks_data):
        priority = subtask_data.get('priority', 'medium').lower()

        # 创建子任务
        subtask = Task(
            project_id=parent_task.project_id,
            title=subtask_data.get('title', f"Subtask {i+1}"),
            content=subtask_data.get('description', ''),
            status=TaskStatus.TODO,
            priority=TaskPriority(priority),
            creator_id=creator_id,
            creator_type='human',
            is_ai_task=True,
            tags=[
                f"parent_task:{parent_task.id}",
                f"subtask_order:{i+1}",
                f"estimated_hours:{subtask_data.get('estimated_hours', '')}"
            ]
        )

        db.session.add(subtask)
        db.session.flush()  # 获取 ID

        created.append({
            'id': subtask.id,
            'order': i + 1,
            'title': subtask.title,
            'description': subtask.content,
            'priority': priority,
            'estimated_hours': subtask_data.get('estimated_hours'),
            'depends_on': subtask_data.get('depends_on', [])
        })

    return created


@ai_task_split_bp.route('/tasks/<int:task_id>/subtasks', methods=['GET'])
@unified_auth_required
def get_subtasks(task_id):
    """
    获取任务的子任务列表
    """
    try:
        # 1. 验证父任务存在
        parent_task = Task.query.get(task_id)
        if not parent_task:
            return ApiResponse.not_found('Task not found').to_response()

        # 2. 获取子任务
        # 注意：JSON 列的 .contains(list) 会把整个单元素数组当 LIKE 子串，
        # 多标签行永远匹配不上；改用带引号定界的子串匹配，
        # 闭口引号保证 parent_task:1 不会误中 parent_task:12
        subtasks = Task.query.filter(
            Task.tags.like(f'%\"parent_task:{task_id}\"%')
        ).order_by(Task.created_at.asc()).all()

        # 3. 构建响应
        subtasks_list = []
        for task in subtasks:
            # 解析标签中的元数据
            order = 0
            estimated_hours = None
            for tag in (task.tags or []):
                if tag.startswith('subtask_order:'):
                    order = int(tag.split(':')[1])
                elif tag.startswith('estimated_hours:'):
                    try:
                        estimated_hours = float(tag.split(':')[1])
                    except:
                        pass

            subtasks_list.append({
                'id': task.id,
                'title': task.title,
                'description': task.content,
                'status': task.status.value if task.status else 'todo',
                'priority': task.priority.value if task.priority else 'medium',
                'order': order,
                'estimated_hours': estimated_hours,
                'created_at': task.created_at.isoformat() if task.created_at else None,
                'updated_at': task.updated_at.isoformat() if task.updated_at else None
            })

        # 4. 按 order 排序
        subtasks_list.sort(key=lambda x: x['order'])

        return ApiResponse.success({
            'parent_task_id': task_id,
            'parent_task_title': parent_task.title,
            'subtasks': subtasks_list,
            'total': len(subtasks_list)
        }, "Subtasks retrieved successfully").to_response()

    except Exception as e:
        return handle_api_error(e)


@ai_task_split_bp.route('/tasks/<int:task_id>/subtasks/<int:subtask_id>', methods=['DELETE'])
@unified_auth_required
def delete_subtask(task_id, subtask_id):
    """
    删除子任务
    """
    try:
        # 1. 获取子任务（引号定界，见 get_subtasks 内注释）
        subtask = Task.query.filter(
            Task.id == subtask_id,
            Task.tags.like(f'%\"parent_task:{task_id}\"%')
        ).first()

        if not subtask:
            return ApiResponse.not_found('Subtask not found').to_response()

        # 2. 权限检查
        user = get_current_user()
        if not user:
            return ApiResponse.error('Authentication required', 401).to_response()

        # TODO: 添加权限检查

        # 3. 删除子任务
        db.session.delete(subtask)

        # 4. 更新父任务标签
        parent_task = Task.query.get(task_id)
        remaining = 0
        if parent_task and parent_task.tags:
            remaining = Task.query.filter(
                Task.tags.like(f'%\"parent_task:{task_id}\"%')
            ).count()

            parent_task.tags = [
                t for t in parent_task.tags
                if not t.startswith('has_subtasks:')
            ]
            if remaining > 0:
                parent_task.tags.append(f"has_subtasks:{remaining}")

        db.session.commit()

        return ApiResponse.success({
            'subtask_id': subtask_id,
            'remaining_subtasks': remaining if parent_task else 0
        }, "Subtask deleted successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e)


@ai_task_split_bp.route('/tasks/<int:task_id>/subtasks/reorder', methods=['PUT'])
@unified_auth_required
def reorder_subtasks(task_id):
    """
    重新排序子任务

    Request Body:
        {
            "orders": [1, 3, 2]  // 子任务的新顺序
        }
    """
    try:
        data = validate_json_request(required_fields=['orders'])
        if isinstance(data, tuple):
            return data

        orders = data.get('orders', [])
        if not isinstance(orders, list):
            return ApiResponse.error('orders must be a list', 400).to_response()

        # 获取子任务（引号定界，见 get_subtasks 内注释）
        subtasks = Task.query.filter(
            Task.tags.like(f'%\"parent_task:{task_id}\"%')
        ).all()

        if len(orders) != len(subtasks):
            return ApiResponse.error(
                'orders length does not match subtasks count',
                400
            ).to_response()

        # 更新顺序
        for i, subtask in enumerate(subtasks):
            # 更新标签中的 order
            if subtask.tags:
                subtask.tags = [
                    t for t in subtask.tags
                    if not t.startswith('subtask_order:')
                ]
                subtask.tags.append(f"subtask_order:{orders[i]}")

        db.session.commit()

        return ApiResponse.success({
            'parent_task_id': task_id,
            'new_orders': orders
        }, "Subtasks reordered successfully").to_response()

    except Exception as e:
        db.session.rollback()
        return handle_api_error(e)
