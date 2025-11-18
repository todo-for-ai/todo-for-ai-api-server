from fastapi import APIRouter, HTTPException, status
from .base import create_success_response, create_error_response

router = APIRouter(prefix="/projects", tags=["projects"])
projects_bp = router

@router.get("/")
async def list_projects():
    """获取项目列表"""
    return create_success_response([])

@router.post("/")
async def create_project():
    """创建项目"""
    return create_success_response({}, "Project created")
