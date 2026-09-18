"""
API基础模块
"""

from typing import Generic, TypeVar, Optional, Dict, Any
from flask import jsonify

T = TypeVar('T')


class ApiResponse(Generic[T]):
    """API响应基类 - 不使用Pydantic，避免与Flask冲突"""

    def __init__(
        self,
        success: bool = True,
        message: str = "Success",
        data: Optional[T] = None,
        error: Optional[str] = None
    ):
        self.success = success
        self.message = message
        self.data = data
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "error": self.error
        }

    def to_response(self, status_code: int = 200):
        """转换为Flask响应"""
        return jsonify(self.to_dict()), status_code

    @classmethod
    def success(cls, data: T = None, message: str = "Success"):
        """创建成功响应"""
        return cls(success=True, message=message, data=data)

    @classmethod
    def error(cls, message: str = "Error", code: int = 500, error: str = None):
        """创建错误响应"""
        return cls(success=False, message=message, error=error)

    @classmethod
    def unauthorized(cls, message: str = "Authentication required"):
        """创建未授权响应"""
        return cls(success=False, message=message)

    @classmethod
    def forbidden(cls, message: str = "Permission denied"):
        """创建禁止访问响应"""
        return cls(success=False, message=message)

    @classmethod
    def not_found(cls, message: str = "Resource not found"):
        """创建未找到响应"""
        return cls(success=False, message=message)


def create_success_response(data: T = None, message: str = "Success") -> ApiResponse[T]:
    """创建成功响应"""
    return ApiResponse(success=True, message=message, data=data)


def create_error_response(error: str, message: str = "Error") -> ApiResponse:
    """创建错误响应"""
    return ApiResponse(success=False, message=message, error=error)


def handle_api_error(e: Exception) -> tuple:
    """处理API错误的通用函数"""
    import traceback
    print(f"API Error: {e}")
    traceback.print_exc()
    # 返回Flask响应对象
    return ApiResponse.error(message=str(e), error=str(e)).to_response(500)


# ============== API 工具函数和异常类 ==============

class APIException(Exception):
    """API异常基类"""
    def __init__(self, message: str, status_code: int = 500, error_code: str = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code or "INTERNAL_ERROR"


def paginate_query(query, page: int = 1, per_page: int = 20):
    """分页查询工具"""
    from flask import request

    # 从请求参数获取分页信息
    if request:
        page = request.args.get('page', page, type=int)
        per_page = request.args.get('per_page', per_page, type=int)

    # 限制每页最大数量
    per_page = min(per_page, 100)
    if per_page < 1:
        per_page = 20
    if page < 1:
        page = 1

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return {
        'items': [item.to_dict() if hasattr(item, 'to_dict') else item for item in pagination.items],
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
        'pages': pagination.pages,
        'has_next': pagination.has_next,
        'has_prev': pagination.has_prev
    }


def validate_json_request(required_fields: list = None):
    """验证JSON请求"""
    from flask import request

    if not request.is_json:
        raise APIException("Content-Type must be application/json", 400, "INVALID_CONTENT_TYPE")

    data = request.get_json()

    if required_fields:
        missing = [field for field in required_fields if field not in data]
        if missing:
            raise APIException(f"Missing required fields: {', '.join(missing)}", 400, "MISSING_FIELDS")

    return data


def get_request_args():
    """获取请求参数"""
    from flask import request

    args = {
        'page': request.args.get('page', 1, type=int),
        'per_page': request.args.get('per_page', 20, type=int),
        'sort_by': request.args.get('sort_by', 'created_at'),
        'sort_order': request.args.get('sort_order', 'desc'),
        'search': request.args.get('search', ''),
        'status': request.args.get('status', ''),
        'project_id': request.args.get('project_id', type=int),
        'priority': request.args.get('priority', ''),
        'task_type': request.args.get('task_type', ''),
    }

    # 限制每页最大数量
    if args['per_page'] > 100:
        args['per_page'] = 100
    if args['per_page'] < 1:
        args['per_page'] = 20

    return args
