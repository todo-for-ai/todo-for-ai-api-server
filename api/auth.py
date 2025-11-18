from fastapi import APIRouter
from .base import create_success_response

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login")
async def login():
    return create_success_response({"token": "dummy"})
