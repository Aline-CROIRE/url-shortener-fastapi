"""Redirect cache: a fast Redis lookup in front of the database."""

from app.cache.redirect_cache import (
    CachedRedirect,
    NullRedirectCache,
    RedirectCache,
    RedisRedirectCache,
)

__all__ = ["CachedRedirect", "NullRedirectCache", "RedirectCache", "RedisRedirectCache"]
