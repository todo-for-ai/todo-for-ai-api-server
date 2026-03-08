import time
from datetime import datetime

from flask import g, jsonify, request

from api.base import handle_api_error

from . import mcp_bp
from .auth import require_api_token_auth
from .handlers import (
    create_task,
    get_project_info,
    get_project_tasks_by_name,
    get_task_by_id,
    list_user_projects,
    submit_task_feedback,
)
from .shared import rate_limit
from .tool_catalog import MCP_TOOLS

TOOL_HANDLERS = {
    'get_project_tasks_by_name': get_project_tasks_by_name,
    'get_task_by_id': get_task_by_id,
    'submit_task_feedback': submit_task_feedback,
    'create_task': create_task,
    'get_project_info': get_project_info,
    'list_user_projects': list_user_projects,
}


@mcp_bp.route('/tools', methods=['GET'])
@require_api_token_auth
@rate_limit(max_requests=60, window_seconds=60)
def list_tools():
    """列出可用的MCP工具"""
    try:
        return jsonify({
            "tools": MCP_TOOLS
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
        handler = TOOL_HANDLERS.get(tool_name)
        if handler is None:
            current_app.logger.error(f"[MCP_TOOL_ERROR] {call_id} Unknown tool: {tool_name}", extra={
                'call_id': call_id,
                'tool_name': tool_name,
                'available_tools': list(TOOL_HANDLERS.keys())
            })
            return jsonify({'error': f'Unknown tool: {tool_name}'}), 400

        result = handler(arguments)

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
