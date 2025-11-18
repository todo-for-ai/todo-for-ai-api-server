from fastapi import APIRouter
from .base import create_success_response

router = APIRouter(prefix="/users", tags=["users"])

@router.get("/")
async def list_users():
    return create_success_response([])
