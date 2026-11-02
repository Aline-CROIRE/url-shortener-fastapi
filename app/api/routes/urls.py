from fastapi import APIRouter
router = APIRouter(prefix="/urls", tags=["URLS"])

@router.get("")

async def get_urls()->dict[str, str]:
    return {"message": "List of URLs"}