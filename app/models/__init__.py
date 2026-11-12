"""All table models, imported in one place.

Importing this package registers every model on Base.metadata, which Alembic
(alembic/env.py) needs to see all tables. Add each new model here.
"""

from app.models.user import User, UserTier

__all__ = ["User", "UserTier"]
