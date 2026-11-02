from fastapi import  FastAPI
from app.api.routes import health
from app.api.router import router


def create_app()->FastAPI:
    app=FastAPI(
        title="URL Shortener API",
        description="A simple URL shortener API built with FastAPI",
        version="0.1.0"
    )
    return app

app=create_app()

app.include_router(router)
app.include_router(health.router)
