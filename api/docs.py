from fastapi import APIRouter

router = APIRouter()

@router.get("/")
async def docs():
    # TODO: Implement
    return {"message": "Not implemented"}
