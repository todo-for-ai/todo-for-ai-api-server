from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def mcp_routes():
    # TODO: Implement
    return {"message": "Not implemented"}
