"""Repositories: the only place that talks to the database with queries.

Routes ask a repository; they never write select()/insert() themselves.
Repositories add and flush, but never commit (one place commits: see F2.2).
"""

from app.repositories.click import ClickRepository
from app.repositories.tag import TagRepository
from app.repositories.url import URLRepository
from app.repositories.user import UserRepository

__all__ = ["ClickRepository", "TagRepository", "URLRepository", "UserRepository"]
