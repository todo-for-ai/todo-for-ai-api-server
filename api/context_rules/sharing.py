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


@context_rules_bp.route('/marketplace', methods=['GET'])
@unified_auth_required
def get_public_rules():
    """获取规则广场的公开规则"""
    try:
        args = _pkg.get_request_args()

        # 获取公开规则
        pagination = _pkg.ContextRule.get_public_rules(
            search=args.get('search'),
            sort_by=args.get('sort_by', 'usage_count'),
            sort_order=args.get('sort_order', 'desc'),
            page=args.get('page', 1),
            per_page=min(args.get('per_page', 20), 100)
        )

        # 转换为字典，包含用户信息
        rules = [rule.to_dict(include_project=True, include_user=True) for rule in pagination.items]

        return ApiResponse.success({
            'items': rules,
            'pagination': {
                'page': pagination.page,
                'per_page': pagination.per_page,
                'total': pagination.total,
                'pages': pagination.pages,
                'has_prev': pagination.has_prev,
                'has_next': pagination.has_next
            }
        }, "Public rules retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve public rules: {str(e)}", 500).to_response()


@context_rules_bp.route('/<int:rule_id>/copy', methods=['POST'])
@unified_auth_required
def copy_rule_from_marketplace(rule_id):
    """从规则广场复制规则"""
    try:
        current_user = _pkg.get_current_user()

        # 获取要复制的规则（必须是公开的）
        source_rule = _pkg.ContextRule.query.filter_by(id=rule_id, is_public=True, is_active=True).first()
        if not source_rule:
            return ApiResponse.error("Public rule not found", 404, error_details={"code": "RULE_NOT_FOUND"}).to_response()

        # 验证请求数据
        data = validate_json_request(
            optional_fields=['name', 'target_project_id', 'copy_as_global']
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 确定复制的名称
        new_name = data.get('name', f"{source_rule.name} - 副本")

        # 确定目标项目ID
        target_project_id = None
        copy_as_global = data.get('copy_as_global', True)

        if not copy_as_global and data.get('target_project_id'):
            target_project_id = data['target_project_id']
            # 验证用户是否有权限访问目标项目
            project = Project.query.get(target_project_id)
            if not project or not current_user.can_access_project(project):
                return ApiResponse.error("Access denied to target project", 403, error_details={"code": "PROJECT_ACCESS_DENIED"}).to_response()

        # 复制规则
        new_rule = source_rule.copy_to_user(
            target_user_id=current_user.id,
            new_name=new_name,
            target_project_id=target_project_id
        )

        return ApiResponse.created(
            new_rule.to_dict(include_project=True),
            "Rule copied successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to copy rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/global', methods=['GET'])
@unified_auth_required
def get_global_context_rules():
    """获取全局上下文规则"""
    try:
        current_user = _pkg.get_current_user()
        args = _pkg.get_request_args()
        cache_key = f"user:{current_user.id}:global:q:{request.query_string.decode('utf-8')}"
        cached = _pkg._context_rules_cache_get(cache_key)
        if cached is not None:
            return ApiResponse.success(cached, "Global context rules retrieved successfully").to_response()

        # 全局规则 = project_id 为空（is_global 的真实含义）。
        # 注意：_pkg.ContextRule.is_global 是 Python property，不能用于 SQL 过滤
        # （原写法 _pkg.ContextRule.is_global == True 恒为 False，导致本人全局规则
        # 永远查不出来）。可见性 = 本人 或 已公开。
        query = _pkg.ContextRule.query.filter(
            db.and_(
                _pkg.ContextRule.project_id.is_(None),
                db.or_(
                    _pkg.ContextRule.is_public == True,
                    _pkg.ContextRule.user_id == current_user.id,
                ),
            )
        )

        # 默认只显示激活的规则；显式 is_active=false 时包含未激活
        # （原写法从 get_request_args 取值，该字典无此键 → 过滤永远生效，
        #  is_active=false 参数形同虚设）
        is_active_param = request.args.get('is_active')
        if is_active_param is None or is_active_param.lower() != 'false':
            query = query.filter(_pkg.ContextRule.is_active == True)

        # 排序
        sort_by = args.get('sort_by', 'priority')
        sort_order = args.get('sort_order', 'desc')

        if sort_by == 'priority':
            if sort_order == 'desc':
                query = query.order_by(_pkg.ContextRule.priority.desc())
            else:
                query = query.order_by(_pkg.ContextRule.priority.asc())
        elif sort_by == 'created_at':
            if sort_order == 'desc':
                query = query.order_by(_pkg.ContextRule.created_at.desc())
            else:
                query = query.order_by(_pkg.ContextRule.created_at.asc())
        elif sort_by == 'name':
            if sort_order == 'desc':
                query = query.order_by(_pkg.ContextRule.name.desc())
            else:
                query = query.order_by(_pkg.ContextRule.name.asc())

        rules = query.all()

        result = [rule.to_dict(include_project=True) for rule in rules]

        _pkg._context_rules_cache_set(cache_key, result)
        return ApiResponse.success(result, "Global context rules retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve global context rules: {str(e)}", 500).to_response()


@context_rules_bp.route('/merged', methods=['GET'])
@unified_auth_required
def get_merged_context_rules():
    """获取合并后的上下文规则（用于AI）"""
    try:
        current_user = _pkg.get_current_user()
        args = _pkg.get_request_args()
        cache_key = f"user:{current_user.id}:merged:q:{request.query_string.decode('utf-8')}"
        cached = _pkg._context_rules_cache_get(cache_key)
        if cached is not None:
            return ApiResponse.success(cached, "Merged context rules retrieved successfully").to_response()

        project_id = args.get('project_id')

        # 构建合并的上下文字符串
        context_string = _pkg.ContextRule.build_context_string(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=True,
            for_projects=True
        )

        # 获取应用的规则列表
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

        result = {
            'content': context_string,
            'rules': [rule.to_dict(include_project=True) for rule in rules]
        }

        _pkg._context_rules_cache_set(cache_key, result)
        return ApiResponse.success(result, "Merged context rules retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve merged context rules: {str(e)}", 500).to_response()
