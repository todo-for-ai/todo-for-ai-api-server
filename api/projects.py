"""
Projects API - Flask Blueprint version
"""
from flask import Blueprint, request
from api.base import ApiResponse, paginate_query, validate_json_request, get_request_args, handle_api_error, APIException
from core.auth import unified_auth_required, get_current_user
from models import db, Project

projects_bp = Blueprint('projects', __name__)


# Import and register routes from chunk files
# For now, implement basic CRUD here

@projects_bp.route('', methods=['GET'])
@unified_auth_required
def list_projects():
    """获取项目列表"""
    try:
        current_user = get_current_user()
        if not current_user:
            return ApiResponse.unauthorized("Authentication required").to_response(401)

        # 获取查询参数
        args = get_request_args()

        # 构建查询 - 查询所有项目（暂不支持按用户过滤）
        query = Project.query

        # 搜索过滤
        if args.get('search'):
            query = query.filter(Project.name.ilike(f"%{args['search']}%"))

        # 排序
        sort_by = args.get('sort_by', 'created_at')
        sort_order = args.get('sort_order', 'desc')
        if hasattr(Project, sort_by):
            sort_column = getattr(Project, sort_by)
            if sort_order == 'desc':
                query = query.order_by(sort_column.desc())
            else:
                query = query.order_by(sort_column.asc())

        # 分页
        result = paginate_query(query, args.get('page', 1), args.get('per_page', 20))

        return ApiResponse.success(result).to_response()

    except Exception as e:
        return handle_api_error(e)


@projects_bp.route('', methods=['POST'])
@unified_auth_required
def create_project():
    """创建项目"""
    try:
        current_user = get_current_user()
        if not current_user:
            return ApiResponse.unauthorized("Authentication required").to_response(401)

        data = validate_json_request(required_fields=['name'])

        # 创建项目
        project = Project(
            name=data['name'],
            description=data.get('description', ''),
            status='active'
        )

        db.session.add(project)
        db.session.commit()

        return ApiResponse.success(
            project.to_dict(),
            "Project created successfully"
        ).to_response(201)

    except APIException as e:
        return ApiResponse.error(e.message, e.status_code).to_response()
    except Exception as e:
        return handle_api_error(e)


@projects_bp.route('/<int:project_id>', methods=['GET'])
@unified_auth_required
def get_project(project_id):
    """获取项目详情"""
    try:
        current_user = get_current_user()
        if not current_user:
            return ApiResponse.unauthorized("Authentication required").to_response(401)

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response(404)

        return ApiResponse.success(project.to_dict()).to_response()

    except Exception as e:
        return handle_api_error(e)


@projects_bp.route('/<int:project_id>', methods=['PUT'])
@unified_auth_required
def update_project(project_id):
    """更新项目"""
    try:
        current_user = get_current_user()
        if not current_user:
            return ApiResponse.unauthorized("Authentication required").to_response(401)

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response(404)

        data = validate_json_request()

        # 更新允许的字段
        allowed_fields = ['name', 'description', 'status']
        for field in allowed_fields:
            if field in data:
                setattr(project, field, data[field])

        db.session.commit()

        return ApiResponse.success(project.to_dict(), "Project updated successfully").to_response()

    except APIException as e:
        return ApiResponse.error(e.message, e.status_code).to_response()
    except Exception as e:
        return handle_api_error(e)


@projects_bp.route('/<int:project_id>', methods=['DELETE'])
@unified_auth_required
def delete_project(project_id):
    """删除项目"""
    try:
        current_user = get_current_user()
        if not current_user:
            return ApiResponse.unauthorized("Authentication required").to_response(401)

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.not_found("Project not found").to_response(404)

        db.session.delete(project)
        db.session.commit()

        return ApiResponse.success(None, "Project deleted successfully").to_response()

    except Exception as e:
        return handle_api_error(e)
