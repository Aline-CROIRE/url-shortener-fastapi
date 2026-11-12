from fastapi import APIRouter

from app.api.routes.analytics import router as analytics_router
from app.api.routes.urls import router as urls_router

router = APIRouter(prefix="/api/v1")
router.include_router(urls_router)
router.include_router(analytics_router)
