"""
上下文规则 API 蓝图

提供上下文规则的 CRUD 操作接口
"""

from datetime import datetime
from flask import Blueprint, request
from models import db, ContextRule, Project
from .base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error
from core.auth import unified_auth_required, get_current_user

# 创建蓝图
context_rules_bp = Blueprint('context_rules', __name__)


@context_rules_bp.route('', methods=['GET'])
@unified_auth_required
def list_context_rules():
    """获取上下文规则列表"""
    try:
        current_user = get_current_user()
        args = get_request_args()

        # 构建查询 - 只返回当前用户的规则
        query = ContextRule.query.filter_by(user_id=current_user.id)

        # 项目筛选
        if args['project_id']:
            project_id = args['project_id']
            # 验证用户是否有权限访问该项目
            project = Project.query.get(project_id)
            if project and not current_user.can_access_project(project):
                return ApiResponse.error("Access denied to project", 403, error_details={"code": "PROJECT_ACCESS_DENIED"}).to_response()
            query = query.filter_by(project_id=project_id)
        elif request.args.get('scope') == 'global':
            query = query.filter_by(project_id=None)
        

        
        # 激活状态筛选
        is_active = request.args.get('is_active')
        if is_active is not None:
            query = query.filter_by(is_active=is_active.lower() == 'true')
        
        # 应用范围筛选
        apply_to_tasks = request.args.get('apply_to_tasks')
        if apply_to_tasks is not None:
            query = query.filter_by(apply_to_tasks=apply_to_tasks.lower() == 'true')
        
        apply_to_projects = request.args.get('apply_to_projects')
        if apply_to_projects is not None:
            query = query.filter_by(apply_to_projects=apply_to_projects.lower() == 'true')
        
        # 搜索
        if args['search']:
            search_term = f"%{args['search']}%"
            query = query.filter(
                ContextRule.name.like(search_term) |
                ContextRule.description.like(search_term) |
                ContextRule.content.like(search_term)
            )
        
        # 排序
        if args['sort_by'] == 'name':
            order_column = ContextRule.name
        elif args['sort_by'] == 'priority':
            order_column = ContextRule.priority
        elif args['sort_by'] == 'rule_type':
            order_column = ContextRule.rule_type
        elif args['sort_by'] == 'updated_at':
            order_column = ContextRule.updated_at
        else:
            order_column = ContextRule.created_at
        
        if args['sort_order'] == 'desc':
            query = query.order_by(order_column.desc())
        else:
            query = query.order_by(order_column.asc())
        
        # 分页
        result = paginate_query(query, args['page'], args['per_page'])
        
        # 包含项目信息
        for item in result['items']:
            if item.get('project_id'):
                project = Project.query.get(item['project_id'])
                if project:
                    item['project'] = {
                        'id': project.id,
                        'name': project.name,
                        'color': project.color
                    }
        
        return ApiResponse.success(result, "Context rules retrieved successfully").to_response()
        
    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve context rules: {str(e)}", 500).to_response()


@context_rules_bp.route('', methods=['POST'])
@unified_auth_required
def create_context_rule():
    """创建新的上下文规则"""
    try:
        current_user = get_current_user()

        # 验证请求数据
        data = validate_json_request(
            required_fields=['name', 'content'],
            optional_fields=[
                'project_id', 'description', 'priority',
                'is_active', 'apply_to_tasks', 'apply_to_projects',
                'is_public'
            ]
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 验证项目是否存在（如果指定了项目ID）
        if data.get('project_id'):
            project = Project.query.get(data['project_id'])
            if not project:
                return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

            # 检查用户是否有权限访问该项目
            if not current_user.can_access_project(project):
                return ApiResponse.error("Access denied to project", 403, error_details={"code": "PROJECT_ACCESS_DENIED"}).to_response()

        # 创建上下文规则
        context_rule = ContextRule.create(
            user_id=current_user.id,
            project_id=data.get('project_id'),
            name=data['name'],
            description=data.get('description', ''),
            content=data['content'],
            priority=data.get('priority', 0),
            is_active=data.get('is_active', True),
            apply_to_tasks=data.get('apply_to_tasks', True),
            apply_to_projects=data.get('apply_to_projects', False),
            is_public=data.get('is_public', False),
            usage_count=0,
            created_by='api'
        )

        db.session.commit()

        return ApiResponse.created(
            context_rule.to_dict(include_project=True),
            "Context rule created successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to create context rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/<int:rule_id>', methods=['GET'])
@unified_auth_required
def get_context_rule(rule_id):
    """获取单个上下文规则详情"""
    try:
        current_user = get_current_user()
        context_rule = ContextRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
        if not context_rule:
            return ApiResponse.error("Context rule not found", 404, error_details={"code": "CONTEXT_RULE_NOT_FOUND"}).to_response()

        return ApiResponse.success(
            context_rule.to_dict(include_project=True),
            "Context rule retrieved successfully"
        ).to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve context rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/<int:rule_id>', methods=['PUT'])
@unified_auth_required
def update_context_rule(rule_id):
    """更新上下文规则"""
    try:
        current_user = get_current_user()
        context_rule = ContextRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
        if not context_rule:
            return ApiResponse.error("Context rule not found", 404, error_details={"code": "CONTEXT_RULE_NOT_FOUND"}).to_response()

        # 验证请求数据
        data = validate_json_request(
            optional_fields=[
                'name', 'description', 'content', 'priority',
                'is_active', 'apply_to_tasks', 'apply_to_projects', 'is_public'
            ]
        )

        if isinstance(data, tuple):  # 错误响应
            return data

        # 更新其他字段
        simple_fields = ['name', 'description', 'content', 'priority', 'is_active', 'apply_to_tasks', 'apply_to_projects', 'is_public']
        for field in simple_fields:
            if field in data:
                setattr(context_rule, field, data[field])

        db.session.commit()

        return ApiResponse.success(
            context_rule.to_dict(include_project=True),
            "Context rule updated successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to update context rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/<int:rule_id>', methods=['DELETE'])
@unified_auth_required
def delete_context_rule(rule_id):
    """删除上下文规则"""
    try:
        current_user = get_current_user()
        context_rule = ContextRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
        if not context_rule:
            return ApiResponse.error("Context rule not found", 404, error_details={"code": "CONTEXT_RULE_NOT_FOUND"}).to_response()

        # 删除规则
        context_rule.delete()

        return ApiResponse.success(None, "Context rule deleted successfully", 204).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to delete context rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/<int:rule_id>/activate', methods=['POST'])
@unified_auth_required
def activate_context_rule(rule_id):
    """激活上下文规则"""
    try:
        current_user = get_current_user()
        context_rule = ContextRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
        if not context_rule:
            return ApiResponse.error("Context rule not found", 404, error_details={"code": "CONTEXT_RULE_NOT_FOUND"}).to_response()

        context_rule.activate()

        return ApiResponse.success(
            context_rule.to_dict(),
            "Context rule activated successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to activate context rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/<int:rule_id>/deactivate', methods=['POST'])
@unified_auth_required
def deactivate_context_rule(rule_id):
    """停用上下文规则"""
    try:
        current_user = get_current_user()
        context_rule = ContextRule.query.filter_by(id=rule_id, user_id=current_user.id).first()
        if not context_rule:
            return ApiResponse.error("Context rule not found", 404, error_details={"code": "CONTEXT_RULE_NOT_FOUND"}).to_response()

        context_rule.deactivate()

        return ApiResponse.success(
            context_rule.to_dict(),
            "Context rule deactivated successfully"
        ).to_response()

    except Exception as e:
        db.session.rollback()
        return ApiResponse.error(f"Failed to deactivate context rule: {str(e)}", 500).to_response()


@context_rules_bp.route('/build-context', methods=['POST'])
@unified_auth_required
def build_context():
    """构建上下文字符串"""
    try:
        current_user = get_current_user()

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
        context_string = ContextRule.build_context_string(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=for_tasks,
            for_projects=for_projects
        )

        # 获取应用的规则列表
        applicable_rules = ContextRule.get_applicable_rules(
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
@context_rules_bp.route('/marketplace', methods=['GET'])
@unified_auth_required
def get_public_rules():
    """获取规则广场的公开规则"""
    try:
        args = get_request_args()

        # 获取公开规则
        pagination = ContextRule.get_public_rules(
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
        current_user = get_current_user()

        # 获取要复制的规则（必须是公开的）
        source_rule = ContextRule.query.filter_by(id=rule_id, is_public=True, is_active=True).first()
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
        current_user = get_current_user()
        args = get_request_args()

        # 构建查询 - 获取全局规则（is_global=True 或 project_id为空且is_public=True）
        query = ContextRule.query.filter(
            db.or_(
                ContextRule.is_global == True,
                db.and_(
                    ContextRule.project_id.is_(None),
                    ContextRule.is_public == True
                )
            )
        )

        # 只显示激活的规则
        if args.get('is_active') is not False:
            query = query.filter(ContextRule.is_active == True)

        # 排序
        sort_by = args.get('sort_by', 'priority')
        sort_order = args.get('sort_order', 'desc')

        if sort_by == 'priority':
            if sort_order == 'desc':
                query = query.order_by(ContextRule.priority.desc())
            else:
                query = query.order_by(ContextRule.priority.asc())
        elif sort_by == 'created_at':
            if sort_order == 'desc':
                query = query.order_by(ContextRule.created_at.desc())
            else:
                query = query.order_by(ContextRule.created_at.asc())
        elif sort_by == 'name':
            if sort_order == 'desc':
                query = query.order_by(ContextRule.name.desc())
            else:
                query = query.order_by(ContextRule.name.asc())

        rules = query.all()

        result = [rule.to_dict(include_project=True) for rule in rules]

        return ApiResponse.success(result, "Global context rules retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve global context rules: {str(e)}", 500).to_response()


@context_rules_bp.route('/merged', methods=['GET'])
@unified_auth_required
def get_merged_context_rules():
    """获取合并后的上下文规则（用于AI）"""
    try:
        current_user = get_current_user()
        args = get_request_args()

        project_id = args.get('project_id')

        # 构建合并的上下文字符串
        context_string = ContextRule.build_context_string(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=True,
            for_projects=True
        )

        # 获取应用的规则列表
        rules_query = ContextRule.query.filter(
            ContextRule.user_id == current_user.id,
            ContextRule.is_active == True
        )

        if project_id:
            # 包含项目特定规则和全局规则
            rules_query = rules_query.filter(
                db.or_(
                    ContextRule.project_id == project_id,
                    ContextRule.project_id.is_(None)
                )
            )
        else:
            # 只包含全局规则
            rules_query = rules_query.filter(ContextRule.project_id.is_(None))

        rules = rules_query.order_by(ContextRule.priority.desc()).all()

        result = {
            'content': context_string,
            'rules': [rule.to_dict(include_project=True) for rule in rules]
        }

        return ApiResponse.success(result, "Merged context rules retrieved successfully").to_response()

    except Exception as e:
        return ApiResponse.error(f"Failed to retrieve merged context rules: {str(e)}", 500).to_response()


@context_rules_bp.route('/preview', methods=['GET'])
@unified_auth_required
def preview_merged_rules():
    """预览合并后的上下文规则"""
    try:
        current_user = get_current_user()
        args = get_request_args()

        project_id = args.get('project_id')

        # 构建预览的上下文字符串（与merged相同的逻辑）
        context_string = ContextRule.build_context_string(
            project_id=project_id,
            user_id=current_user.id,
            for_tasks=True,
            for_projects=True
        )

        # 获取将要应用的规则列表
        rules_query = ContextRule.query.filter(
            ContextRule.user_id == current_user.id,
            ContextRule.is_active == True
        )

        if project_id:
            # 包含项目特定规则和全局规则
            rules_query = rules_query.filter(
                db.or_(
                    ContextRule.project_id == project_id,
                    ContextRule.project_id.is_(None)
                )
            )
        else:
            # 只包含全局规则
            rules_query = rules_query.filter(ContextRule.project_id.is_(None))

        rules = rules_query.order_by(ContextRule.priority.desc()).all()

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
