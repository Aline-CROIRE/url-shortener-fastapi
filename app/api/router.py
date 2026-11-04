from fastapi import APIRouter
from app.api.routes.urls import router as urls_router

router = APIRouter(prefix="/api/v1")
router.include_router(urls_router)
