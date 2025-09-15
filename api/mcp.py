"""
MCP (Model Context Protocol) HTTP API接口
"""

import json
import asyncio
from flask import Blueprint, request, jsonify, g
from models import db, Project, Task, TaskStatus, ContextRule, ApiToken
from api.base import handle_api_error
from core.github_config import require_auth
from datetime import datetime, timedelta
from functools import wraps
import html
import re
from collections import defaultdict
import time

mcp_bp = Blueprint('mcp', __name__)

# 简单的内存频率限制器
rate_limiter = defaultdict(list)


def rate_limit(max_requests=10, window_seconds=60):
    """频率限制装饰器"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 获取客户端标识（IP地址或用户ID）
            client_id = request.remote_addr
            if hasattr(g, 'current_user') and g.current_user:
                client_id = f"user_{g.current_user.id}"

            current_time = time.time()

            # 清理过期的请求记录
            rate_limiter[client_id] = [
                req_time for req_time in rate_limiter[client_id]
                if current_time - req_time < window_seconds
            ]

            # 检查是否超过限制
            if len(rate_limiter[client_id]) >= max_requests:
                return jsonify({
                    'error': 'Rate limit exceeded',
                    'message': f'Maximum {max_requests} requests per {window_seconds} seconds'
                }), 429

            # 记录当前请求
            rate_limiter[client_id].append(current_time)

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_api_token_auth(f):
    """API Token认证装饰器 - 专门用于MCP接口"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from flask import current_app

        auth_start_time = time.time()
        auth_id = f"auth-{int(time.time() * 1000)}-{id(request)}"

        current_app.logger.debug(f"[AUTH_START] {auth_id} API token authentication started", extra={
            'auth_id': auth_id,
            'endpoint': request.endpoint,
            'method': request.method,
            'path': request.path,
            'remote_addr': request.remote_addr,
            'user_agent': request.headers.get('User-Agent', 'Unknown')
        })

        # 从请求头获取token
        auth_header = request.headers.get('Authorization')

        current_app.logger.debug(f"[AUTH_HEADER] {auth_id} Authorization header check", extra={
            'auth_id': auth_id,
            'has_auth_header': bool(auth_header),
            'header_format_valid': bool(auth_header and auth_header.startswith('Bearer ')) if auth_header else False
        })

        if not auth_header or not auth_header.startswith('Bearer '):
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Missing or invalid authorization header", extra={
                'auth_id': auth_id,
                'auth_header_present': bool(auth_header),
                'auth_header_format': auth_header[:20] + '...' if auth_header and len(auth_header) > 20 else auth_header
            })
            return jsonify({'error': 'Missing or invalid authorization header'}), 401

        token = auth_header.split(' ')[1]
        token_prefix = token[:8] + '...' if len(token) > 8 else token

        current_app.logger.debug(f"[AUTH_TOKEN] {auth_id} Token extracted", extra={
            'auth_id': auth_id,
            'token_prefix': token_prefix,
            'token_length': len(token)
        })

        # 验证token
        token_verify_start = time.time()
        api_token = ApiToken.verify_token(token)
        token_verify_duration = time.time() - token_verify_start

        current_app.logger.debug(f"[AUTH_VERIFY] {auth_id} Token verification completed", extra={
            'auth_id': auth_id,
            'token_prefix': token_prefix,
            'verify_duration_ms': round(token_verify_duration * 1000, 2),
            'token_valid': bool(api_token),
            'token_id': api_token.id if api_token else None,
            'user_id': api_token.user.id if api_token and api_token.user else None
        })

        if not api_token:
            current_app.logger.warning(f"[AUTH_FAILED] {auth_id} Invalid or expired token", extra={
                'auth_id': auth_id,
                'token_prefix': token_prefix,
                'verify_duration_ms': round(token_verify_duration * 1000, 2)
            })
            return jsonify({'error': 'Invalid or expired token'}), 401

        # 将token信息添加到g对象
        g.api_token = api_token
        g.current_user = api_token.user

        auth_duration = time.time() - auth_start_time
        current_app.logger.info(f"[AUTH_SUCCESS] {auth_id} Authentication successful", extra={
            'auth_id': auth_id,
            'auth_duration_ms': round(auth_duration * 1000, 2),
            'token_id': api_token.id,
            'user_id': api_token.user.id,
            'user_email': api_token.user.email if hasattr(api_token.user, 'email') else None,
            'token_name': api_token.name if hasattr(api_token, 'name') else None
        })

        return f(*args, **kwargs)

    return decorated_function


def sanitize_input(text):
    """清理输入，防止XSS攻击"""
    if not isinstance(text, str):
        return text

    # HTML转义
    text = html.escape(text)

    # 移除潜在的脚本标签
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'javascript:', '', text, flags=re.IGNORECASE)
    text = re.sub(r'on\w+\s*=', '', text, flags=re.IGNORECASE)

    return text


def validate_integer(value, field_name):
    """验证整数输入"""
    if isinstance(value, int):
        return value

    if isinstance(value, str) and value.isdigit():
        return int(value)

    raise ValueError(f"{field_name} must be a valid integer")


@mcp_bp.route('/tools', methods=['GET'])
@require_api_token_auth
@rate_limit(max_requests=60, window_seconds=60)
def list_tools():
    """列出可用的MCP工具"""
    try:
        tools = [
            {
                "name": "get_project_tasks_by_name",
                "description": "Get all pending tasks for a project by project name, sorted by creation time",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "The name of the project to get tasks for"
                        },
                        "status_filter": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["todo", "in_progress", "review"]
                            },
                            "description": "Filter tasks by status (default: todo, in_progress, review)",
                            "default": ["todo", "in_progress", "review"]
                        }
                    },
                    "required": ["project_name"]
                }
            },
            {
                "name": "get_task_by_id",
                "description": "Get detailed task information by task ID",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": "The ID of the task to retrieve"
                        }
                    },
                    "required": ["task_id"]
                }
            },
            {
                "name": "create_task",
                "description": "Create a new task in the specified project",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "integer",
                            "description": "The ID of the project to create the task in"
                        },
                        "title": {
                            "type": "string",
                            "description": "The title of the task"
                        },
                        "content": {
                            "type": "string",
                            "description": "The detailed content/description of the task"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["todo", "in_progress", "review", "done", "cancelled"],
                            "description": "The initial status of the task (default: todo)",
                            "default": "todo"
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "urgent"],
                            "description": "The priority of the task (default: medium)",
                            "default": "medium"
                        },
                        "assignee": {
                            "type": "string",
                            "description": "The person assigned to this task (optional)"
                        },
                        "due_date": {
                            "type": "string",
                            "description": "The due date in YYYY-MM-DD format (optional)"
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags associated with the task (optional)"
                        },
                        "related_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files related to this task (optional)"
                        },
                        "is_ai_task": {
                            "type": "boolean",
                            "description": "Whether this task was created by AI (default: true)",
                            "default": True
                        },
                        "ai_identifier": {
                            "type": "string",
                            "description": "Identifier of the AI creating the task (optional)"
                        }
                    },
                    "required": ["project_id", "title"]
                }
            },
            {
                "name": "update_task",
                "description": "Update an existing task with proper permission checking",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": "The ID of the task to update"
                        },
                        "title": {
                            "type": "string",
                            "description": "The new title of the task (optional)"
                        },
                        "content": {
                            "type": "string",
                            "description": "The new content/description of the task (optional)"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["todo", "in_progress", "review", "done", "cancelled"],
                            "description": "The new status of the task (optional)"
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "urgent"],
                            "description": "The new priority of the task (optional)"
                        },
                        "due_date": {
                            "type": "string",
                            "description": "The new due date in YYYY-MM-DD format (optional)"
                        },
                        "completion_rate": {
                            "type": "number",
                            "description": "The completion rate percentage (0-100) (optional)"
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags associated with the task (optional)"
                        }
                    },
                    "required": ["task_id"]
                }
            },
            {
                "name": "get_project_info",
                "description": "Get detailed project information including statistics and configuration. Provide either project_id or project_name.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "integer",
                            "description": "The ID of the project to retrieve (optional if project_name is provided)"
                        },
                        "project_name": {
                            "type": "string",
                            "description": "The name of the project to retrieve (optional if project_id is provided)"
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "list_user_projects",
                "description": "List all projects that the current user has access to, with proper permission checking",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status_filter": {
                            "type": "string",
                            "enum": ["active", "archived", "all"],
                            "description": "Filter projects by status (default: active)",
                            "default": "active"
                        },
                        "include_stats": {
                            "type": "boolean",
                            "description": "Whether to include project statistics (default: false)",
                            "default": False
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "submit_task_feedback",
                "description": "Submit feedback for a completed or in-progress task",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": "The ID of the task to provide feedback for"
                        },
                        "project_name": {
                            "type": "string",
                            "description": "The name of the project this task belongs to"
                        },
                        "feedback_content": {
                            "type": "string",
                            "description": "The feedback content describing what was done"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["in_progress", "review", "done", "cancelled"],
                            "description": "The new status of the task after feedback"
                        },
                        "ai_identifier": {
                            "type": "string",
                            "description": "Identifier of the AI providing feedback (optional)"
                        }
                    },
                    "required": ["task_id", "project_name", "feedback_content", "status"]
                }
            },
            {
                "name": "wait_for_new_tasks",
                "description": "Wait for new tasks to be created in a project, with configurable timeout and polling interval",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "The name of the project to monitor for new tasks"
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "description": "Maximum time to wait for new tasks in seconds (default: 3600, max: 7200)",
                            "default": 3600,
                            "minimum": 30,
                            "maximum": 7200
                        },
                        "poll_interval_seconds": {
                            "type": "number",
                            "description": "Interval between checks for new tasks in seconds (default: 30, min: 10)",
                            "default": 30,
                            "minimum": 10,
                            "maximum": 300
                        }
                    },
                    "required": ["project_name"]
                }
            },
            {
                "name": "wait_for_human_feedback",
                "description": "Wait for human feedback on an interactive task that AI has submitted for review",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "integer",
                            "description": "The ID of the task to wait for human feedback"
                        },
                        "session_id": {
                            "type": "string",
                            "description": "The interaction session ID"
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "description": "Maximum time to wait for human feedback in seconds (default: 3600, max: 7200)",
                            "default": 3600,
                            "minimum": 30,
                            "maximum": 7200
                        },
                        "poll_interval_seconds": {
                            "type": "number",
                            "description": "Interval between checks for human feedback in seconds (default: 30, min: 10)",
                            "default": 30,
                            "minimum": 10,
                            "maximum": 300
                        }
                    },
                    "required": ["task_id", "session_id"]
                }
            }
        ]
        
        return jsonify({
            "tools": tools
        })
    
    except Exception as e:
        return handle_api_error(e)


@mcp_bp.route('/call', methods=['POST'])
@require_api_token_auth
@rate_limit(max_requests=60, window_seconds=60)
def call_tool():
    """调用MCP工具"""
    import logging
    from flask import current_app

    call_start_time = time.time()
    call_id = f"mcp-call-{int(time.time() * 1000)}-{id(request)}"

    current_app.logger.info(f"[MCP_CALL_START] {call_id} MCP tool call initiated", extra={
        'call_id': call_id,
        'request_id': getattr(g, 'request_id', 'unknown'),
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'api_token_id': g.api_token.id if hasattr(g, 'api_token') and g.api_token else None,
        'remote_addr': request.remote_addr,
        'user_agent': request.headers.get('User-Agent', 'Unknown'),
        'timestamp': datetime.utcnow().isoformat()
    })

    try:
        data = request.get_json()

        current_app.logger.debug(f"[MCP_CALL_DATA] {call_id} Request data received", extra={
            'call_id': call_id,
            'has_data': bool(data),
            'data_size': len(str(data)) if data else 0,
            'data_keys': list(data.keys()) if data else []
        })

        if not data:
            current_app.logger.warning(f"[MCP_CALL_ERROR] {call_id} No data provided")
            return jsonify({'error': 'No data provided'}), 400

        tool_name = data.get('name')
        arguments = data.get('arguments', {})

        current_app.logger.info(f"[MCP_CALL_TOOL] {call_id} Tool call details", extra={
            'call_id': call_id,
            'tool_name': tool_name,
            'has_arguments': bool(arguments),
            'arguments_keys': list(arguments.keys()) if arguments else [],
            'arguments_size': len(str(arguments)) if arguments else 0
        })

        if not tool_name:
            current_app.logger.warning(f"[MCP_CALL_ERROR] {call_id} Tool name is required")
            return jsonify({'error': 'Tool name is required'}), 400

        # 记录工具调用开始
        tool_start_time = time.time()
        current_app.logger.info(f"[MCP_TOOL_START] {call_id} Executing tool: {tool_name}", extra={
            'call_id': call_id,
            'tool_name': tool_name,
            'arguments': arguments,
            'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None
        })

        # 调用对应的工具函数
        result = None
        if tool_name == 'get_project_tasks_by_name':
            result = get_project_tasks_by_name(arguments)
        elif tool_name == 'get_task_by_id':
            result = get_task_by_id(arguments)
        elif tool_name == 'submit_task_feedback':
            result = submit_task_feedback(arguments)
        elif tool_name == 'create_task':
            result = create_task(arguments)
        elif tool_name == 'update_task':
            result = update_task(arguments)
        elif tool_name == 'get_project_info':
            result = get_project_info(arguments)
        elif tool_name == 'list_user_projects':
            result = list_user_projects(arguments)
        elif tool_name == 'wait_for_new_tasks':
            result = wait_for_new_tasks(arguments)
        elif tool_name == 'wait_for_human_feedback':
            result = wait_for_human_feedback(arguments)
        else:
            current_app.logger.error(f"[MCP_TOOL_ERROR] {call_id} Unknown tool: {tool_name}", extra={
                'call_id': call_id,
                'tool_name': tool_name,
                'available_tools': ['get_project_tasks_by_name', 'get_task_by_id', 'submit_task_feedback', 'create_task', 'update_task', 'get_project_info', 'list_user_projects', 'wait_for_new_tasks', 'wait_for_human_feedback']
            })
            return jsonify({'error': f'Unknown tool: {tool_name}'}), 400

        tool_duration = time.time() - tool_start_time
        total_duration = time.time() - call_start_time

        # 检查结果中是否有错误
        has_error = isinstance(result, dict) and 'error' in result

        if has_error:
            current_app.logger.warning(f"[MCP_TOOL_ERROR] {call_id} Tool returned error", extra={
                'call_id': call_id,
                'tool_name': tool_name,
                'tool_duration_ms': round(tool_duration * 1000, 2),
                'total_duration_ms': round(total_duration * 1000, 2),
                'error': result.get('error'),
                'result': result
            })
        else:
            current_app.logger.info(f"[MCP_TOOL_SUCCESS] {call_id} Tool executed successfully", extra={
                'call_id': call_id,
                'tool_name': tool_name,
                'tool_duration_ms': round(tool_duration * 1000, 2),
                'total_duration_ms': round(total_duration * 1000, 2),
                'result_size': len(str(result)) if result else 0,
                'result_type': type(result).__name__,
                'result_keys': list(result.keys()) if isinstance(result, dict) else []
            })

        current_app.logger.info(f"[MCP_CALL_END] {call_id} MCP tool call completed", extra={
            'call_id': call_id,
            'tool_name': tool_name,
            'success': not has_error,
            'total_duration_ms': round(total_duration * 1000, 2),
            'status_code': 400 if has_error else 200
        })

        return jsonify(result)

    except Exception as e:
        total_duration = time.time() - call_start_time
        current_app.logger.error(f"[MCP_CALL_EXCEPTION] {call_id} Exception during MCP tool call", extra={
            'call_id': call_id,
            'tool_name': tool_name if 'tool_name' in locals() else 'unknown',
            'total_duration_ms': round(total_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e),
            'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None
        }, exc_info=True)
        return handle_api_error(e)


def get_project_tasks_by_name(arguments):
    """根据项目名称获取任务列表"""
    project_name = arguments.get('project_name')
    status_filter = arguments.get('status_filter', ['todo', 'in_progress', 'review'])

    if not project_name:
        return {'error': 'project_name is required'}

    # 清理输入
    project_name = sanitize_input(project_name)

    # 查找项目
    project = Project.query.filter_by(name=project_name).first()
    if not project:
        # 只返回当前用户有权限访问的项目
        user_projects = Project.query.filter_by(owner_id=g.current_user.id).all()
        return {
            'error': f'Project "{project_name}" not found',
            'available_projects': [p.name for p in user_projects]
        }

    # 检查权限 - 只能访问自己创建的项目
    if project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only access your own projects'}
    
    # 获取任务
    query = Task.query.filter_by(project_id=project.id)
    if status_filter:
        query = query.filter(Task.status.in_(status_filter))
    
    tasks = query.order_by(Task.created_at.asc()).all()
    
    tasks_data = []
    for task in tasks:
        task_dict = task.to_dict()
        task_dict['project_name'] = project.name
        tasks_data.append(task_dict)
    
    return {
        'project_name': project.name,
        'project_id': project.id,
        'status_filter': status_filter,
        'total_tasks': len(tasks_data),
        'tasks': tasks_data
    }


def get_task_by_id(arguments):
    """根据任务ID获取任务详情"""
    task_id = arguments.get('task_id')

    if not task_id:
        return {'error': 'task_id is required'}

    # 验证task_id是整数
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    # 检查权限 - 只能访问自己创建的任务或自己项目中的任务
    if task.creator_id != g.current_user.id:
        # 检查是否是项目创建者
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return {'error': 'Access denied: You can only access your own tasks'}

    # 获取项目信息
    project = Project.query.get(task.project_id)

    task_data = task.to_dict()
    task_data['project_name'] = project.name if project else None
    task_data['project_description'] = project.description if project else None

    # 获取项目级别的上下文规则并拼接到任务内容后
    if project:
        # 获取任务创建者的用户ID
        task_user_id = task.creator_id if task.creator_id else None

        project_context = ContextRule.build_context_string(
            project_id=project.id,
            user_id=task_user_id,
            for_tasks=True,
            for_projects=False
        )

        if project_context:
            # 将项目上下文拼接到任务内容后
            original_content = task_data.get('content', '')
            task_data['content'] = f"{original_content}\n\n## 项目上下文规则\n\n{project_context}"

    return task_data


def submit_task_feedback(arguments):
    """提交任务反馈 - 支持交互式任务"""
    import uuid
    from models import InteractionLog, InteractionType, InteractionStatus
    from flask import current_app

    task_id = arguments.get('task_id')
    project_name = arguments.get('project_name')
    feedback_content = arguments.get('feedback_content')
    status = arguments.get('status')
    ai_identifier = arguments.get('ai_identifier', 'AI Assistant')

    if not all([task_id, project_name, feedback_content, status]):
        return {'error': 'task_id, project_name, feedback_content, and status are required'}

    # 验证和清理输入
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    project_name = sanitize_input(project_name)
    feedback_content = sanitize_input(feedback_content)
    ai_identifier = sanitize_input(ai_identifier)

    # 验证状态值
    valid_statuses = ['in_progress', 'review', 'done', 'cancelled', 'waiting_human_feedback']
    if status not in valid_statuses:
        return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}

    # 验证任务存在并属于指定项目
    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    project = Project.query.get(task.project_id)
    if not project or project.name != project_name:
        return {'error': f'Task {task_id} does not belong to project "{project_name}"'}

    # 检查权限 - 只能修改自己创建的任务或自己项目中的任务
    if task.creator_id != g.current_user.id and project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only modify your own tasks'}

    # 跟踪状态变更
    old_status = task.status
    status_changed = str(old_status) != str(status)

    # 检查是否为交互式任务
    is_interactive_task = task.is_interactive

    # 生成或使用现有的交互会话ID
    session_id = task.interaction_session_id
    if not session_id:
        session_id = str(uuid.uuid4())
        task.interaction_session_id = session_id

    current_app.logger.info(f"[SUBMIT_TASK_FEEDBACK] Processing feedback for task {task_id}", extra={
        'task_id': task_id,
        'is_interactive': is_interactive_task,
        'session_id': session_id,
        'status': status,
        'ai_identifier': ai_identifier
    })

    # 更新任务基本信息
    task.feedback_content = feedback_content
    task.feedback_at = datetime.utcnow()

    # 处理交互式任务逻辑
    if is_interactive_task and status != 'cancelled':
        # 创建AI反馈记录
        interaction_log = InteractionLog.create_ai_feedback(
            task_id=task_id,
            session_id=session_id,
            content=feedback_content,
            metadata={
                'ai_identifier': ai_identifier,
                'original_status': str(old_status),
                'requested_status': status
            }
        )

        # 如果AI请求完成任务，设置为等待人工反馈状态
        if status == 'done':
            task.status = 'waiting_human_feedback'
            task.ai_waiting_feedback = True
            current_app.logger.info(f"[INTERACTIVE_TASK] Task {task_id} set to waiting_human_feedback")
        else:
            # 对于其他状态，直接更新
            task.status = status
            task.ai_waiting_feedback = False
    else:
        # 非交互式任务，直接更新状态
        task.status = status
        task.ai_waiting_feedback = False

    # 更新项目最后活动时间
    project.last_activity_at = datetime.utcnow()

    try:
        db.session.commit()
        current_app.logger.info(f"[SUBMIT_TASK_FEEDBACK] Successfully updated task {task_id}")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[SUBMIT_TASK_FEEDBACK] Failed to update task {task_id}: {str(e)}")
        return {'error': f'Failed to submit feedback: {str(e)}'}

    # 记录用户活跃度
    user_id = None
    if task.creator_id:
        user_id = task.creator_id
    elif project.owner_id:
        user_id = project.owner_id

    if user_id:
        from models import UserActivity
        try:
            if status_changed:
                UserActivity.record_activity(user_id, 'task_status_changed')
                # 如果任务状态变为完成，额外记录完成任务活跃度
                if task.status == 'done':
                    UserActivity.record_activity(user_id, 'task_completed')
            else:
                UserActivity.record_activity(user_id, 'task_updated')
        except Exception as e:
            print(f"Warning: Failed to record user activity: {str(e)}")

    # 构建返回结果
    result = {
        'task_id': task_id,
        'project_name': project_name,
        'status': task.status.value if hasattr(task.status, 'value') else task.status,
        'feedback_submitted': True,
        'feedback_content': feedback_content,
        'ai_identifier': ai_identifier,
        'timestamp': datetime.utcnow().isoformat(),
        'is_interactive': is_interactive_task,
        'session_id': session_id
    }

    # 如果是交互式任务且AI在等待反馈，添加等待信息
    if is_interactive_task and task.ai_waiting_feedback:
        result['waiting_human_feedback'] = True
        result['message'] = 'Task feedback submitted. Waiting for human confirmation or additional instructions.'

    return result


def create_task(arguments):
    """创建新任务"""
    project_id = arguments.get('project_id')
    title = arguments.get('title')
    content = arguments.get('content', '')
    status = arguments.get('status', 'todo')
    priority = arguments.get('priority', 'medium')
    assignee = arguments.get('assignee')
    due_date = arguments.get('due_date')
    tags = arguments.get('tags', [])
    related_files = arguments.get('related_files', [])
    is_ai_task = arguments.get('is_ai_task', True)
    creator_identifier = arguments.get('ai_identifier', 'MCP Client')

    if not project_id:
        return {'error': 'project_id is required'}

    if not title:
        return {'error': 'title is required'}

    # 清理输入
    title = sanitize_input(title)
    content = sanitize_input(content) if content else ''
    assignee = sanitize_input(assignee) if assignee else None
    creator_identifier = sanitize_input(creator_identifier) if creator_identifier else 'MCP Client'

    # 验证项目存在且用户有权限
    project = Project.query.filter_by(id=project_id).first()
    if not project:
        return {'error': f'Project with ID {project_id} not found'}

    # 检查权限 - 只能在自己创建的项目中创建任务
    if project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only create tasks in your own projects'}

    # 验证状态值
    valid_statuses = ['todo', 'in_progress', 'review', 'done', 'cancelled']
    if status not in valid_statuses:
        return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}

    # 验证优先级值
    valid_priorities = ['low', 'medium', 'high', 'urgent']
    if priority not in valid_priorities:
        return {'error': f'Invalid priority. Must be one of: {", ".join(valid_priorities)}'}

    # 解析due_date
    due_date_obj = None
    if due_date:
        try:
            due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()
        except ValueError:
            return {'error': 'Invalid due_date format. Use YYYY-MM-DD'}

    try:
        # 创建任务
        task = Task(
            title=title,
            content=content,
            status=status,
            priority=priority,
            project_id=project_id,
            creator_id=g.current_user.id,
            assignee=assignee,
            due_date=due_date_obj,
            is_ai_task=is_ai_task,
            creator_identifier=creator_identifier,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )

        db.session.add(task)
        db.session.commit()

        # 注意：标签和相关文件功能暂时不支持，因为相关模型尚未实现
        # 这些参数会被保存在返回结果中，但不会存储到数据库

        # 记录用户活跃度
        from models import UserActivity
        try:
            UserActivity.record_activity(g.current_user.id, 'task_created')
        except Exception as e:
            print(f"Warning: Failed to record user activity: {str(e)}")

        # 返回创建的任务信息
        return {
            'id': task.id,
            'title': task.title,
            'content': task.content,
            'status': task.status.value if hasattr(task.status, 'value') else task.status,
            'priority': task.priority.value if hasattr(task.priority, 'value') else task.priority,
            'project_id': task.project_id,
            'project_name': project.name,
            'creator_id': task.creator_id,
            'assignee': task.assignee,
            'due_date': task.due_date.isoformat() if task.due_date else None,
            'is_ai_task': task.is_ai_task,
            'creator_identifier': task.creator_identifier,
            'created_at': task.created_at.isoformat(),
            'updated_at': task.updated_at.isoformat(),
            'tags': tags,
            'related_files': related_files
        }

    except Exception as e:
        db.session.rollback()
        return {'error': f'Failed to create task: {str(e)}'}


def get_project_info(arguments):
    """获取项目详细信息"""
    from flask import current_app

    func_start_time = time.time()
    func_id = f"get-project-info-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[GET_PROJECT_INFO_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    project_id = arguments.get('project_id')
    project_name = arguments.get('project_name')

    current_app.logger.debug(f"[GET_PROJECT_INFO_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'project_id': project_id,
        'project_name': project_name,
        'has_project_id': bool(project_id),
        'has_project_name': bool(project_name)
    })

    if not project_id and not project_name:
        current_app.logger.warning(f"[GET_PROJECT_INFO_ERROR] {func_id} Missing required arguments")
        return {'error': 'Either project_id or project_name is required'}

    # 查找项目
    query_start_time = time.time()
    if project_id:
        current_app.logger.debug(f"[GET_PROJECT_INFO_QUERY] {func_id} Querying by project_id: {project_id}")
        project = Project.query.filter_by(id=project_id).first()
    else:
        project_name = sanitize_input(project_name)
        current_app.logger.debug(f"[GET_PROJECT_INFO_QUERY] {func_id} Querying by project_name: {project_name}")
        project = Project.query.filter_by(name=project_name).first()

    query_duration = time.time() - query_start_time
    current_app.logger.debug(f"[GET_PROJECT_INFO_QUERY_RESULT] {func_id} Query completed", extra={
        'func_id': func_id,
        'query_duration_ms': round(query_duration * 1000, 2),
        'project_found': bool(project),
        'project_id': project.id if project else None,
        'project_name': project.name if project else None
    })

    if not project:
        # 只返回当前用户有权限访问的项目
        user_projects_query_start = time.time()
        user_projects = Project.query.filter_by(owner_id=g.current_user.id).all()
        user_projects_query_duration = time.time() - user_projects_query_start

        identifier = f'ID {project_id}' if project_id else f'name "{project_name}"'

        current_app.logger.warning(f"[GET_PROJECT_INFO_NOT_FOUND] {func_id} Project not found", extra={
            'func_id': func_id,
            'identifier': identifier,
            'user_projects_count': len(user_projects),
            'user_projects_query_duration_ms': round(user_projects_query_duration * 1000, 2),
            'available_projects': [{'id': p.id, 'name': p.name} for p in user_projects]
        })

        return {
            'error': f'Project with {identifier} not found',
            'available_projects': [{'id': p.id, 'name': p.name} for p in user_projects]
        }

    # 检查权限 - 只能访问自己创建的项目
    if project.owner_id != g.current_user.id:
        current_app.logger.warning(f"[GET_PROJECT_INFO_ACCESS_DENIED] {func_id} Access denied", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'project_owner_id': project.owner_id,
            'current_user_id': g.current_user.id
        })
        return {'error': 'Access denied: You can only access your own projects'}

    try:
        # 获取项目统计信息
        stats_start_time = time.time()
        current_app.logger.debug(f"[GET_PROJECT_INFO_STATS] {func_id} Starting statistics queries")

        total_tasks = Task.query.filter_by(project_id=project.id).count()
        todo_tasks = Task.query.filter_by(project_id=project.id, status='todo').count()
        in_progress_tasks = Task.query.filter_by(project_id=project.id, status='in_progress').count()
        review_tasks = Task.query.filter_by(project_id=project.id, status='review').count()
        done_tasks = Task.query.filter_by(project_id=project.id, status='done').count()
        cancelled_tasks = Task.query.filter_by(project_id=project.id, status='cancelled').count()

        stats_duration = time.time() - stats_start_time
        current_app.logger.debug(f"[GET_PROJECT_INFO_STATS_RESULT] {func_id} Statistics queries completed", extra={
            'func_id': func_id,
            'stats_duration_ms': round(stats_duration * 1000, 2),
            'total_tasks': total_tasks,
            'todo_tasks': todo_tasks,
            'in_progress_tasks': in_progress_tasks,
            'review_tasks': review_tasks,
            'done_tasks': done_tasks,
            'cancelled_tasks': cancelled_tasks
        })

        # 获取最近的任务
        recent_tasks_start_time = time.time()
        current_app.logger.debug(f"[GET_PROJECT_INFO_RECENT] {func_id} Querying recent tasks")

        recent_tasks = Task.query.filter_by(project_id=project.id)\
                          .order_by(Task.updated_at.desc())\
                          .limit(5)\
                          .all()

        recent_tasks_duration = time.time() - recent_tasks_start_time
        current_app.logger.debug(f"[GET_PROJECT_INFO_RECENT_RESULT] {func_id} Recent tasks query completed", extra={
            'func_id': func_id,
            'recent_tasks_duration_ms': round(recent_tasks_duration * 1000, 2),
            'recent_tasks_count': len(recent_tasks)
        })

        recent_tasks_data = []
        for task in recent_tasks:
            recent_tasks_data.append({
                'id': task.id,
                'title': task.title,
                'status': task.status.value if hasattr(task.status, 'value') else task.status,
                'priority': task.priority.value if hasattr(task.priority, 'value') else task.priority,
                'updated_at': task.updated_at.isoformat()
            })

        completion_rate = round((done_tasks / total_tasks * 100) if total_tasks > 0 else 0, 2)

        result = {
            'id': project.id,
            'name': project.name,
            'description': project.description,
            'status': project.status.value if hasattr(project.status, 'value') else getattr(project, 'status', 'active'),
            'github_url': project.github_url,
            'project_context': project.project_context,
            'owner_id': project.owner_id,
            'created_at': project.created_at.isoformat(),
            'updated_at': project.updated_at.isoformat(),
            'total_tasks': total_tasks,
            'pending_tasks': todo_tasks + in_progress_tasks + review_tasks,
            'completed_tasks': done_tasks,
            'completion_rate': completion_rate,
            'statistics': {
                'total_tasks': total_tasks,
                'todo_tasks': todo_tasks,
                'in_progress_tasks': in_progress_tasks,
                'review_tasks': review_tasks,
                'done_tasks': done_tasks,
                'cancelled_tasks': cancelled_tasks,
                'completion_rate': completion_rate
            },
            'recent_tasks': recent_tasks_data
        }

        func_duration = time.time() - func_start_time
        current_app.logger.info(f"[GET_PROJECT_INFO_SUCCESS] {func_id} Function completed successfully", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'func_duration_ms': round(func_duration * 1000, 2),
            'result_size': len(str(result)),
            'total_tasks': total_tasks,
            'completion_rate': completion_rate
        })

        return result

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[GET_PROJECT_INFO_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'project_id': project.id if 'project' in locals() and project else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to get project info: {str(e)}'}


def list_user_projects(arguments):
    """列出用户有权限访问的所有项目"""
    from flask import current_app

    func_start_time = time.time()
    func_id = f"list-user-projects-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[LIST_USER_PROJECTS_START] {func_id} Starting to list user projects", extra={
        'func_id': func_id,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'arguments': arguments,
        'timestamp': datetime.utcnow().isoformat()
    })

    try:
        # 获取参数
        status_filter = arguments.get('status_filter', 'active')
        include_stats = arguments.get('include_stats', False)

        current_app.logger.debug(f"[LIST_USER_PROJECTS_PARAMS] {func_id} Parameters parsed", extra={
            'func_id': func_id,
            'status_filter': status_filter,
            'include_stats': include_stats,
            'user_id': g.current_user.id
        })

        # 构建查询 - 只返回当前用户拥有的项目
        query_start_time = time.time()
        query = Project.query.filter_by(owner_id=g.current_user.id)

        # 根据状态筛选
        if status_filter == 'active':
            from models.project import ProjectStatus
            query = query.filter_by(status=ProjectStatus.ACTIVE)
        elif status_filter == 'archived':
            from models.project import ProjectStatus
            query = query.filter_by(status=ProjectStatus.ARCHIVED)
        elif status_filter == 'all':
            # 不过滤状态，但排除已删除的项目
            from models.project import ProjectStatus
            query = query.filter(Project.status != ProjectStatus.DELETED)

        # 按最后活动时间排序，如果没有则按创建时间排序
        # MySQL不支持NULLS LAST，使用COALESCE处理NULL值
        from sqlalchemy import func
        projects = query.order_by(
            func.coalesce(Project.last_activity_at, Project.created_at).desc(),
            Project.created_at.desc()
        ).all()

        query_duration = time.time() - query_start_time

        current_app.logger.debug(f"[LIST_USER_PROJECTS_QUERY] {func_id} Projects query completed", extra={
            'func_id': func_id,
            'projects_count': len(projects),
            'query_duration_ms': round(query_duration * 1000, 2),
            'status_filter': status_filter
        })

        # 构建返回数据
        projects_data = []
        for project in projects:
            project_dict = {
                'id': project.id,
                'name': project.name,
                'description': project.description,
                'color': project.color,
                'status': project.status.value if hasattr(project.status, 'value') else getattr(project, 'status', 'active'),
                'github_url': project.github_url,
                'local_url': project.local_url,
                'production_url': project.production_url,
                'project_context': project.project_context,
                'owner_id': project.owner_id,
                'created_at': project.created_at.isoformat(),
                'updated_at': project.updated_at.isoformat(),
                'last_activity_at': project.last_activity_at.isoformat() if project.last_activity_at else None
            }

            # 如果需要包含统计信息
            if include_stats:
                stats_start_time = time.time()

                # 获取任务统计
                from models.task import TaskStatus
                total_tasks = project.tasks.count()
                todo_tasks = project.tasks.filter_by(status=TaskStatus.TODO).count()
                in_progress_tasks = project.tasks.filter_by(status=TaskStatus.IN_PROGRESS).count()
                review_tasks = project.tasks.filter_by(status=TaskStatus.REVIEW).count()
                done_tasks = project.tasks.filter_by(status=TaskStatus.DONE).count()
                cancelled_tasks = project.tasks.filter_by(status=TaskStatus.CANCELLED).count()

                # 计算完成率
                completion_rate = (done_tasks / total_tasks * 100) if total_tasks > 0 else 0

                # 获取上下文规则数量
                context_rules_count = project.context_rules.filter_by(is_active=True).count()

                stats_duration = time.time() - stats_start_time

                project_dict.update({
                    'total_tasks': total_tasks,
                    'pending_tasks': todo_tasks + in_progress_tasks + review_tasks,
                    'completed_tasks': done_tasks,
                    'completion_rate': round(completion_rate, 2),
                    'context_rules_count': context_rules_count,
                    'statistics': {
                        'total_tasks': total_tasks,
                        'todo_tasks': todo_tasks,
                        'in_progress_tasks': in_progress_tasks,
                        'review_tasks': review_tasks,
                        'done_tasks': done_tasks,
                        'cancelled_tasks': cancelled_tasks,
                        'completion_rate': round(completion_rate, 2),
                        'context_rules_count': context_rules_count
                    }
                })

                current_app.logger.debug(f"[LIST_USER_PROJECTS_STATS] {func_id} Stats calculated for project {project.id}", extra={
                    'func_id': func_id,
                    'project_id': project.id,
                    'project_name': project.name,
                    'stats_duration_ms': round(stats_duration * 1000, 2),
                    'total_tasks': total_tasks,
                    'completion_rate': round(completion_rate, 2)
                })

            projects_data.append(project_dict)

        func_duration = time.time() - func_start_time

        result = {
            'projects': projects_data,
            'total': len(projects_data),
            'status_filter': status_filter,
            'include_stats': include_stats,
            'user_id': g.current_user.id
        }

        current_app.logger.info(f"[LIST_USER_PROJECTS_SUCCESS] {func_id} Successfully listed user projects", extra={
            'func_id': func_id,
            'projects_count': len(projects_data),
            'status_filter': status_filter,
            'include_stats': include_stats,
            'func_duration_ms': round(func_duration * 1000, 2),
            'user_id': g.current_user.id
        })

        return result

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[LIST_USER_PROJECTS_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to list user projects: {str(e)}'}


def wait_for_new_tasks(arguments):
    """等待项目中的新任务"""
    import time
    from flask import current_app

    func_start_time = time.time()
    func_id = f"wait-for-new-tasks-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    project_name = arguments.get('project_name')
    timeout_seconds = arguments.get('timeout_seconds', 3600)  # Default 1 hour
    poll_interval_seconds = arguments.get('poll_interval_seconds', 30)  # Default 30 seconds

    current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'project_name': project_name,
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds,
        'has_project_name': bool(project_name)
    })

    if not project_name:
        current_app.logger.warning(f"[WAIT_FOR_NEW_TASKS_ERROR] {func_id} Missing required arguments")
        return {'error': 'project_name is required'}

    # 验证和清理输入
    project_name = sanitize_input(project_name)

    # 验证超时时间和轮询间隔
    try:
        timeout_seconds = max(30, min(7200, int(timeout_seconds)))  # 30秒到2小时
        poll_interval_seconds = max(10, min(300, int(poll_interval_seconds)))  # 10秒到5分钟
    except (ValueError, TypeError):
        return {'error': 'timeout_seconds and poll_interval_seconds must be valid numbers'}

    # 查找项目
    query_start_time = time.time()
    project = Project.query.filter_by(name=project_name).first()
    query_duration = time.time() - query_start_time

    current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_QUERY] {func_id} Project query completed", extra={
        'func_id': func_id,
        'query_duration_ms': round(query_duration * 1000, 2),
        'project_found': bool(project),
        'project_id': project.id if project else None,
        'project_name': project.name if project else None
    })

    if not project:
        # 只返回当前用户有权限访问的项目
        user_projects = Project.query.filter_by(owner_id=g.current_user.id).all()
        return {
            'error': f'Project "{project_name}" not found',
            'available_projects': [p.name for p in user_projects]
        }

    # 检查权限 - 只能访问自己创建的项目
    if project.owner_id != g.current_user.id:
        current_app.logger.warning(f"[WAIT_FOR_NEW_TASKS_ACCESS_DENIED] {func_id} Access denied", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'project_owner_id': project.owner_id,
            'current_user_id': g.current_user.id
        })
        return {'error': 'Access denied: You can only access your own projects'}

    # 记录开始时间戳，用于检测新任务
    start_timestamp = datetime.utcnow()
    end_time = time.time() + timeout_seconds
    poll_count = 0

    current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_POLLING_START] {func_id} Starting polling loop", extra={
        'func_id': func_id,
        'project_id': project.id,
        'project_name': project.name,
        'start_timestamp': start_timestamp.isoformat(),
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds,
        'end_time': datetime.fromtimestamp(end_time).isoformat()
    })

    try:
        while time.time() < end_time:
            poll_count += 1
            poll_start_time = time.time()

            current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_POLL] {func_id} Poll #{poll_count} started", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'remaining_time': end_time - time.time(),
                'timestamp': datetime.utcnow().isoformat()
            })

            # 查询在开始时间之后创建的待执行任务
            new_tasks_query = Task.query.filter(
                Task.project_id == project.id,
                Task.created_at > start_timestamp,
                Task.status.in_(['todo', 'in_progress', 'review'])
            ).order_by(Task.created_at.asc())

            new_tasks = new_tasks_query.all()
            poll_duration = time.time() - poll_start_time

            current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_POLL_RESULT] {func_id} Poll #{poll_count} completed", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'poll_duration_ms': round(poll_duration * 1000, 2),
                'new_tasks_count': len(new_tasks),
                'new_task_ids': [task.id for task in new_tasks] if new_tasks else []
            })

            if new_tasks:
                # 找到新任务，准备返回结果
                tasks_data = []
                for task in new_tasks:
                    task_dict = task.to_dict()
                    task_dict['project_name'] = project.name
                    tasks_data.append(task_dict)

                func_duration = time.time() - func_start_time

                current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_SUCCESS] {func_id} Found new tasks", extra={
                    'func_id': func_id,
                    'project_id': project.id,
                    'project_name': project.name,
                    'new_tasks_count': len(new_tasks),
                    'poll_count': poll_count,
                    'func_duration_ms': round(func_duration * 1000, 2),
                    'task_ids': [task.id for task in new_tasks]
                })

                return {
                    'project_name': project.name,
                    'project_id': project.id,
                    'new_tasks': tasks_data,
                    'total_new_tasks': len(tasks_data),
                    'poll_count': poll_count,
                    'wait_duration_seconds': round(func_duration, 2),
                    'timeout': False,
                    'start_timestamp': start_timestamp.isoformat(),
                    'found_timestamp': datetime.utcnow().isoformat()
                }

            # 没有找到新任务，检查是否还有时间继续轮询
            remaining_time = end_time - time.time()
            if remaining_time <= 0:
                break

            # 等待下一次轮询，但不超过剩余时间
            sleep_time = min(poll_interval_seconds, remaining_time)
            if sleep_time > 0:
                current_app.logger.debug(f"[WAIT_FOR_NEW_TASKS_SLEEP] {func_id} Sleeping before next poll", extra={
                    'func_id': func_id,
                    'poll_count': poll_count,
                    'sleep_time': sleep_time,
                    'remaining_time': remaining_time
                })
                time.sleep(sleep_time)

        # 超时，没有找到新任务
        func_duration = time.time() - func_start_time

        current_app.logger.info(f"[WAIT_FOR_NEW_TASKS_TIMEOUT] {func_id} Wait timed out", extra={
            'func_id': func_id,
            'project_id': project.id,
            'project_name': project.name,
            'poll_count': poll_count,
            'func_duration_ms': round(func_duration * 1000, 2),
            'timeout_seconds': timeout_seconds
        })

        return {
            'project_name': project.name,
            'project_id': project.id,
            'new_tasks': [],
            'total_new_tasks': 0,
            'poll_count': poll_count,
            'wait_duration_seconds': round(func_duration, 2),
            'timeout': True,
            'timeout_seconds': timeout_seconds,
            'start_timestamp': start_timestamp.isoformat(),
            'end_timestamp': datetime.utcnow().isoformat()
        }

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[WAIT_FOR_NEW_TASKS_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'project_id': project.id if 'project' in locals() and project else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'poll_count': poll_count,
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to wait for new tasks: {str(e)}'}


def update_task(arguments):
    """更新现有任务"""
    from flask import current_app
    import requests

    func_start_time = time.time()
    func_id = f"update-task-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[UPDATE_TASK_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    task_id = arguments.get('task_id')

    if not task_id:
        current_app.logger.warning(f"[UPDATE_TASK_ERROR] {func_id} Missing required task_id")
        return {'error': 'task_id is required'}

    # 验证task_id是整数
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        current_app.logger.warning(f"[UPDATE_TASK_ERROR] {func_id} Invalid task_id: {str(e)}")
        return {'error': str(e)}

    current_app.logger.debug(f"[UPDATE_TASK_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'task_id': task_id,
        'has_title': 'title' in arguments,
        'has_content': 'content' in arguments,
        'has_status': 'status' in arguments,
        'has_priority': 'priority' in arguments,
        'has_due_date': 'due_date' in arguments
    })

    # 构建更新数据
    update_data = {}
    
    # 清理和验证输入
    if 'title' in arguments and arguments['title']:
        update_data['title'] = sanitize_input(arguments['title'])
    
    if 'content' in arguments and arguments['content'] is not None:
        update_data['content'] = sanitize_input(arguments['content'])
    
    if 'status' in arguments and arguments['status']:
        valid_statuses = ['todo', 'in_progress', 'review', 'done', 'cancelled']
        status = arguments['status']
        if status not in valid_statuses:
            return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}
        update_data['status'] = status
    
    if 'priority' in arguments and arguments['priority']:
        valid_priorities = ['low', 'medium', 'high', 'urgent']
        priority = arguments['priority']
        if priority not in valid_priorities:
            return {'error': f'Invalid priority. Must be one of: {", ".join(valid_priorities)}'}
        update_data['priority'] = priority
    
    if 'due_date' in arguments and arguments['due_date']:
        # 验证日期格式
        try:
            datetime.strptime(arguments['due_date'], '%Y-%m-%d')
            update_data['due_date'] = arguments['due_date']
        except ValueError:
            return {'error': 'Invalid due_date format. Use YYYY-MM-DD'}
    
    if 'completion_rate' in arguments and arguments['completion_rate'] is not None:
        try:
            completion_rate = float(arguments['completion_rate'])
            if completion_rate < 0 or completion_rate > 100:
                return {'error': 'completion_rate must be between 0 and 100'}
            update_data['completion_rate'] = completion_rate
        except (ValueError, TypeError):
            return {'error': 'completion_rate must be a valid number'}
    
    if 'tags' in arguments and arguments['tags'] is not None:
        if isinstance(arguments['tags'], list):
            update_data['tags'] = [sanitize_input(tag) for tag in arguments['tags'] if tag]
        else:
            return {'error': 'tags must be an array of strings'}

    # 如果没有提供任何要更新的字段
    if not update_data:
        return {'error': 'At least one field must be provided for update'}

    current_app.logger.debug(f"[UPDATE_TASK_DATA] {func_id} Update data prepared", extra={
        'func_id': func_id,
        'task_id': task_id,
        'update_fields': list(update_data.keys()),
        'update_data_size': len(str(update_data))
    })

    try:
        # 直接调用后端 tasks API
        # 构建内部 API 请求头，包含当前用户的认证信息
        headers = {
            'Content-Type': 'application/json',
            'Authorization': request.headers.get('Authorization')  # 传递原始的认证头
        }
        
        # 获取当前应用的基础 URL
        base_url = request.host_url.rstrip('/')
        tasks_api_url = f"{base_url}/api/tasks/{task_id}"
        
        current_app.logger.debug(f"[UPDATE_TASK_API_CALL] {func_id} Calling tasks API", extra={
            'func_id': func_id,
            'api_url': tasks_api_url,
            'headers': {k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()},
            'update_data': update_data
        })

        api_call_start_time = time.time()
        
        # 调用内部 tasks API
        response = requests.put(
            tasks_api_url,
            json=update_data,
            headers=headers,
            timeout=30
        )
        
        api_call_duration = time.time() - api_call_start_time

        current_app.logger.debug(f"[UPDATE_TASK_API_RESPONSE] {func_id} Tasks API response received", extra={
            'func_id': func_id,
            'api_call_duration_ms': round(api_call_duration * 1000, 2),
            'status_code': response.status_code,
            'has_response_data': bool(response.content)
        })

        if response.status_code == 200:
            result = response.json()
            
            # 检查响应格式
            if 'data' in result:
                task_data = result['data']
            else:
                task_data = result
            
            func_duration = time.time() - func_start_time
            
            current_app.logger.info(f"[UPDATE_TASK_SUCCESS] {func_id} Task updated successfully", extra={
                'func_id': func_id,
                'task_id': task_id,
                'func_duration_ms': round(func_duration * 1000, 2),
                'updated_fields': list(update_data.keys()),
                'task_title': task_data.get('title', 'Unknown')
            })
            
            return task_data
        else:
            # API 调用失败
            error_data = {}
            try:
                error_data = response.json()
            except:
                error_data = {'error': f'HTTP {response.status_code}: {response.text}'}
            
            current_app.logger.warning(f"[UPDATE_TASK_API_ERROR] {func_id} Tasks API returned error", extra={
                'func_id': func_id,
                'task_id': task_id,
                'status_code': response.status_code,
                'error_data': error_data
            })
            
            return {'error': error_data.get('error', f'Failed to update task: HTTP {response.status_code}')}

    except requests.RequestException as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[UPDATE_TASK_REQUEST_ERROR] {func_id} Request exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to call tasks API: {str(e)}'}
    
    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[UPDATE_TASK_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to update task: {str(e)}'}


def wait_for_human_feedback(arguments):
    """等待人工反馈 - 用于交互式任务"""
    import time
    from models import InteractionLog, InteractionType, InteractionStatus
    from flask import current_app

    func_start_time = time.time()
    func_id = f"wait-for-human-feedback-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    task_id = arguments.get('task_id')
    session_id = arguments.get('session_id')
    timeout_seconds = arguments.get('timeout_seconds', 3600)  # Default 1 hour
    poll_interval_seconds = arguments.get('poll_interval_seconds', 30)  # Default 30 seconds

    current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'task_id': task_id,
        'session_id': session_id,
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds
    })

    if not all([task_id, session_id]):
        current_app.logger.warning(f"[WAIT_FOR_HUMAN_FEEDBACK_ERROR] {func_id} Missing required arguments")
        return {'error': 'task_id and session_id are required'}

    # 验证输入
    try:
        task_id = validate_integer(task_id, 'task_id')
        timeout_seconds = max(30, min(7200, int(timeout_seconds)))  # 30秒到2小时
        poll_interval_seconds = max(10, min(300, int(poll_interval_seconds)))  # 10秒到5分钟
    except (ValueError, TypeError) as e:
        return {'error': f'Invalid input: {str(e)}'}

    session_id = sanitize_input(session_id)

    # 验证任务存在
    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    # 检查权限
    if task.creator_id != g.current_user.id:
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return {'error': 'Access denied: You can only access your own tasks'}

    # 验证任务是交互式的且在等待反馈
    if not task.is_interactive:
        return {'error': 'Task is not interactive'}

    if not task.ai_waiting_feedback:
        return {'error': 'Task is not waiting for human feedback'}

    if task.interaction_session_id != session_id:
        return {'error': 'Invalid session ID'}

    # 开始轮询等待人工反馈
    end_time = time.time() + timeout_seconds
    poll_count = 0

    current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_POLLING_START] {func_id} Starting polling loop", extra={
        'func_id': func_id,
        'task_id': task_id,
        'session_id': session_id,
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds
    })

    try:
        while time.time() < end_time:
            poll_count += 1
            poll_start_time = time.time()

            current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_POLL] {func_id} Poll #{poll_count} started", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'remaining_time': end_time - time.time()
            })

            # 刷新任务状态
            db.session.refresh(task)

            # 检查是否有新的人工响应
            human_responses = InteractionLog.query.filter(
                InteractionLog.session_id == session_id,
                InteractionLog.interaction_type == InteractionType.HUMAN_RESPONSE,
                InteractionLog.created_at > datetime.utcnow() - timedelta(seconds=timeout_seconds + 60)
            ).order_by(InteractionLog.created_at.desc()).all()

            poll_duration = time.time() - poll_start_time

            current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_POLL_RESULT] {func_id} Poll #{poll_count} completed", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'poll_duration_ms': round(poll_duration * 1000, 2),
                'human_responses_count': len(human_responses),
                'ai_waiting_feedback': task.ai_waiting_feedback
            })

            # 如果任务不再等待反馈，说明有人工响应
            if not task.ai_waiting_feedback or human_responses:
                latest_response = human_responses[0] if human_responses else None

                func_duration = time.time() - func_start_time

                current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_SUCCESS] {func_id} Received human feedback", extra={
                    'func_id': func_id,
                    'task_id': task_id,
                    'session_id': session_id,
                    'poll_count': poll_count,
                    'func_duration_ms': round(func_duration * 1000, 2),
                    'response_status': latest_response.status.value if latest_response else 'unknown'
                })

                result = {
                    'task_id': task_id,
                    'session_id': session_id,
                    'human_feedback_received': True,
                    'poll_count': poll_count,
                    'wait_duration_seconds': round(func_duration, 2),
                    'timeout': False,
                    'task_status': task.status.value if hasattr(task.status, 'value') else task.status,
                    'ai_waiting_feedback': task.ai_waiting_feedback
                }

                if latest_response:
                    result['human_response'] = {
                        'content': latest_response.content,
                        'status': latest_response.status.value if hasattr(latest_response.status, 'value') else latest_response.status,
                        'created_at': latest_response.created_at.isoformat(),
                        'created_by': latest_response.created_by
                    }

                    # 根据人工响应状态决定下一步行动
                    if latest_response.status == InteractionStatus.COMPLETED:
                        result['action'] = 'task_completed'
                        result['message'] = 'Task has been marked as completed by human reviewer.'
                    elif latest_response.status == InteractionStatus.CONTINUED:
                        result['action'] = 'continue_task'
                        result['message'] = 'Human has provided additional instructions. Continue working on the task.'
                        result['additional_instructions'] = latest_response.content
                    else:
                        result['action'] = 'pending'
                        result['message'] = 'Human response received but status is pending.'

                return result

            # 没有收到人工反馈，检查是否还有时间继续轮询
            remaining_time = end_time - time.time()
            if remaining_time <= 0:
                break

            # 等待下一次轮询
            sleep_time = min(poll_interval_seconds, remaining_time)
            if sleep_time > 0:
                current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_SLEEP] {func_id} Sleeping before next poll", extra={
                    'func_id': func_id,
                    'poll_count': poll_count,
                    'sleep_time': sleep_time,
                    'remaining_time': remaining_time
                })
                time.sleep(sleep_time)

        # 超时，没有收到人工反馈
        func_duration = time.time() - func_start_time

        current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_TIMEOUT] {func_id} Wait timed out", extra={
            'func_id': func_id,
            'task_id': task_id,
            'session_id': session_id,
            'poll_count': poll_count,
            'func_duration_ms': round(func_duration * 1000, 2),
            'timeout_seconds': timeout_seconds
        })

        return {
            'task_id': task_id,
            'session_id': session_id,
            'human_feedback_received': False,
            'poll_count': poll_count,
            'wait_duration_seconds': round(func_duration, 2),
            'timeout': True,
            'timeout_seconds': timeout_seconds,
            'task_status': task.status.value if hasattr(task.status, 'value') else task.status,
            'ai_waiting_feedback': task.ai_waiting_feedback,
            'message': 'Timeout waiting for human feedback. Task remains in waiting state.'
        }

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[WAIT_FOR_HUMAN_FEEDBACK_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'session_id': session_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'poll_count': poll_count,
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to wait for human feedback: {str(e)}'}
