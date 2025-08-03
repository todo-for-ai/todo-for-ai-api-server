"""
API 基础工具函数

包含通用的响应格式、错误处理等工具函数
"""

from datetime import datetime
from flask import jsonify, request
from typing import Any, Optional, Dict


class ApiResponse:
    """
    统一的API响应类

    标准响应格式：
    {
        "code": 200,
        "message": "Success",
        "data": {...},
        "timestamp": "2025-08-03T10:00:00.000000",
        "path": "/api/v1/projects"
    }
    """

    def __init__(self,
                 data: Any = None,
                 message: str = "Success",
                 code: int = 200,
                 timestamp: Optional[str] = None,
                 path: Optional[str] = None,
                 **kwargs):
        self.code = code
        self.message = message
        self.data = data
        self.timestamp = timestamp or datetime.utcnow().isoformat()
        self.path = path or (request.path if request else None)

        # 支持额外的字段（如pagination等）
        for key, value in kwargs.items():
            setattr(self, key, value)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            'code': self.code,
            'message': self.message,
            'timestamp': self.timestamp,
            'path': self.path
        }

        # 只有当data不为None时才添加data字段
        if self.data is not None:
            result['data'] = self.data

        # 添加其他额外字段
        for key, value in self.__dict__.items():
            if key not in ['code', 'message', 'data', 'timestamp', 'path']:
                result[key] = value

        return result

    def to_response(self):
        """转换为Flask响应对象"""
        return jsonify(self.to_dict()), self.code

    @classmethod
    def success(cls, data: Any = None, message: str = "Success", code: int = 200, **kwargs):
        """创建成功响应"""
        return cls(data=data, message=message, code=code, **kwargs)

    @classmethod
    def error(cls, message: str = "An error occurred", code: int = 400, **kwargs):
        """创建错误响应"""
        return cls(data=None, message=message, code=code, **kwargs)

    @classmethod
    def created(cls, data: Any = None, message: str = "Created successfully", **kwargs):
        """创建201响应"""
        return cls(data=data, message=message, code=201, **kwargs)

    @classmethod
    def not_found(cls, message: str = "Resource not found", **kwargs):
        """创建404响应"""
        return cls(data=None, message=message, code=404, **kwargs)

    @classmethod
    def forbidden(cls, message: str = "Access denied", **kwargs):
        """创建403响应"""
        return cls(data=None, message=message, code=403, **kwargs)

    @classmethod
    def unauthorized(cls, message: str = "Authentication required", **kwargs):
        """创建401响应"""
        return cls(data=None, message=message, code=401, **kwargs)





def paginate_query(query, page=1, per_page=20, max_per_page=100):
    """
    分页查询工具函数
    
    Args:
        query: SQLAlchemy 查询对象
        page: 页码
        per_page: 每页数量
        max_per_page: 最大每页数量
    
    Returns:
        分页结果字典
    """
    # 限制每页数量
    per_page = min(per_page, max_per_page)
    
    # 执行分页查询
    pagination = query.paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )
    
    return {
        'items': [item.to_dict() for item in pagination.items],
        'pagination': {
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'pages': pagination.pages,
            'has_prev': pagination.has_prev,
            'has_next': pagination.has_next,
            'prev_num': pagination.prev_num,
            'next_num': pagination.next_num
        }
    }


def validate_json_request(required_fields=None, optional_fields=None):
    """
    验证 JSON 请求数据
    
    Args:
        required_fields: 必需字段列表
        optional_fields: 可选字段列表
    
    Returns:
        验证后的数据字典或错误响应
    """
    if not request.is_json:
        return ApiResponse.error("Content-Type must be application/json", 400).to_response()

    data = request.get_json()
    if not data:
        return ApiResponse.error("Request body must contain valid JSON", 400).to_response()
    
    # 检查必需字段
    if required_fields:
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return ApiResponse.error(
                f"Missing required fields: {', '.join(missing_fields)}",
                400,
                error_details={
                    "code": "MISSING_FIELDS",
                    "details": {"missing_fields": missing_fields}
                }
            ).to_response()
    
    # 过滤允许的字段
    allowed_fields = set()
    if required_fields:
        allowed_fields.update(required_fields)
    if optional_fields:
        allowed_fields.update(optional_fields)
    
    if allowed_fields:
        filtered_data = {k: v for k, v in data.items() if k in allowed_fields}
        return filtered_data
    
    return data


def get_request_args():
    """
    获取请求参数
    
    Returns:
        包含分页和筛选参数的字典
    """
    return {
        'page': request.args.get('page', 1, type=int),
        'per_page': request.args.get('per_page', 20, type=int),
        'search': request.args.get('search', '').strip(),
        'sort_by': request.args.get('sort_by', 'created_at'),
        'sort_order': request.args.get('sort_order', 'desc'),
        'status': request.args.get('status'),
        'priority': request.args.get('priority'),
        'assignee': request.args.get('assignee'),
        'project_id': request.args.get('project_id', type=int),
    }


class APIException(Exception):
    """自定义 API 异常类"""
    
    def __init__(self, message, status_code=400, error_code=None, details=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details
    
    def to_response(self):
        """转换为 API 错误响应"""
        kwargs = {}
        if self.error_code or self.details:
            kwargs['error_details'] = {}
            if self.error_code:
                kwargs['error_details']['code'] = self.error_code
            if self.details:
                kwargs['error_details']['details'] = self.details

        return ApiResponse.error(
            message=self.message,
            code=self.status_code,
            **kwargs
        ).to_response()


def handle_api_error(error, status_code=500):
    """
    处理API错误的通用函数

    Args:
        error: 错误对象或错误消息
        status_code: HTTP状态码

    Returns:
        Flask Response 对象
    """
    if isinstance(error, Exception):
        message = str(error)
    else:
        message = error

    return ApiResponse.error(message, status_code).to_response()
