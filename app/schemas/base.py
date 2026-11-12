from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    """Base class for every OUTPUT schema.

    from_attributes=True lets Pydantic read values from object attributes
    (url.short_code) instead of dict keys (url["short_code"]). That works for
    SQLAlchemy models and for SQL result rows with .label() names.
    """

    model_config = ConfigDict(from_attributes=True)
