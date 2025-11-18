"""
API基础模块
"""

from typing import Generic, TypeVar, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from flask import jsonify

T = TypeVar('T')

class ApiResponse(BaseModel, Generic[T]):
    """API响应基类"""
    success: bool = True
    message: str = "Success"
    data: Optional[T] = None
    error: Optional[str] = None

    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "message": "Success",
                "data": None
            }
        }

    def to_response(self, status_code: int = 200):
        """转换为Flask响应"""
        return jsonify(self.dict()), status_code

    @staticmethod
    def success(data: T = None, message: str = "Success"):
        """创建成功响应"""
        return ApiResponse(success=True, message=message, data=data)

    @staticmethod
    def error(message: str = "Error", code: int = 500, error: str = None):
        """创建错误响应"""
        return ApiResponse(success=False, message=message, error=error)

    @staticmethod
    def unauthorized(message: str = "Authentication required"):
        """创建未授权响应"""
        return ApiResponse(success=False, message=message)

    @staticmethod
    def forbidden(message: str = "Permission denied"):
        """创建禁止访问响应"""
        return ApiResponse(success=False, message=message)

    @staticmethod
    def not_found(message: str = "Resource not found"):
        """创建未找到响应"""
        return ApiResponse(success=False, message=message)

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

router = APIRouter()

@router.get("/")
async def base():
    """基础端点"""
    return create_success_response({"status": "API is running"})
