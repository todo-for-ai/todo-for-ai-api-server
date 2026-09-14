"""
上下文规则 API 蓝图

提供上下文规则的 CRUD 操作接口
"""

from datetime import datetime
from flask import Blueprint, request
from models import db, ContextRule, Project
from ..base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
from core.auth import unified_auth_required, get_current_user
from core.redis_client import get_json as redis_get_json, set_json as redis_set_json
from core.cache_invalidation import invalidate_user_caches

from flask import Blueprint

from api import context_rules as _pkg
from api.context_rules._core import (
    context_rules_bp,
    CONTEXT_RULES_CACHE_TTL_SECONDS,
    context_rules_fallback_cache,
    _context_rules_cache_get,
    _context_rules_cache_set,
)


@context_rules_bp.route('/build-context', methods=['POST'])
@unified_auth_required
def build_context():
    """构建上下文字符串"""
    try:
        current_user = _pkg.get_current_user()

        # 验证请求数据
        data = validate_json_request(
            optional_fields=['project_id', 'for_tasks', 'for_projects']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        project_id = data.get('project_id')
        for_tasks = data.get('for_tasks', True)
        for_projects = data.get('for_projects', False)

        # 构建上下文字符串（只包含当前用户的规则）
        context_string = _pkg.ContextRule.build_context_string(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=for_tasks,
            for_projects=for_projects
        )

        # 获取应用的规则列表
        applicable_rules = _pkg.ContextRule.get_applicable_rules(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=for_tasks,
            for_projects=for_projects
        )
        
        return ApiResponse.success(
            {
                'context_string': context_string,
                'rules_applied': len(applicable_rules),
                'rules': [rule.to_dict() for rule in applicable_rules]
            },
            "Context built successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to build context: {str(e)}", 500).to_response()


# 规则广场相关API


@context_rules_bp.route('/preview', methods=['GET'])
@unified_auth_required
def preview_merged_rules():
    """预览合并后的上下文规则"""
    try:
        current_user = _pkg.get_current_user()
        args = _pkg.get_request_args()

        project_id = args.get('project_id')

        # 构建预览的上下文字符串（与merged相同的逻辑）
        context_string = _pkg.ContextRule.build_context_string(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=True,
            for_projects=True
        )

        # 获取将要应用的规则列表
        rules_query = _pkg.ContextRule.query.filter(
            _pkg.ContextRule.user_id == current_user.id,
            _pkg.ContextRule.is_active == True
        )

        if project_id:
            # 包含项目特定规则和全局规则
            rules_query = rules_query.filter(
                db.or_(
                    _pkg.ContextRule.project_id == project_id,
                    _pkg.ContextRule.project_id.is_(None)
                )
            )
        else:
            # 只包含全局规则
            rules_query = rules_query.filter(_pkg.ContextRule.project_id.is_(None))

        rules = rules_query.order_by(_pkg.ContextRule.priority.desc()).all()

        # 添加预览特定的信息
        result = {
            'content': context_string,
            'rules': [rule.to_dict(include_project=True) for rule in rules],
            'preview_info': {
                'total_rules': len(rules),
                'project_id': project_id,
                'content_length': len(context_string),
                'generated_at': datetime.utcnow().isoformat()
            }
        }

        return ApiResponse.success(result, "Context rules preview generated successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to generate context rules preview: {str(e)}", 500).to_response()
