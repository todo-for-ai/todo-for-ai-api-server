from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def tokens():
    # TODO: Implement
    return {"message": "Not implemented"}
