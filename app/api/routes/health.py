from fastapi import APIRouter

router = APIRouter(tags=["Health"])

@router.get("/health")
async def check_health() -> dict[str, str]:
    return {"status": "healthy"}