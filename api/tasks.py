from fastapi import APIRouter, HTTPException, status
from .base import create_success_response, create_error_response

router = APIRouter(prefix="/tasks", tags=["tasks"])
tasks_bp = router

@router.get("/")
async def list_tasks():
    """获取任务列表"""
    return create_success_response([])

@router.post("/")
async def create_task():
    """创建任务"""
    return create_success_response({}, "Task created")
