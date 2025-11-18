from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def api_tokens():
    # TODO: Implement
    return {"message": "Not implemented"}
