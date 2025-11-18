from fastapi import APIRouter
from .base import create_success_response

router = APIRouter(prefix="/context-rules", tags=["context-rules"])
context_rules_bp = router

@router.get("/")
async def list_context_rules():
    return create_success_response([])
