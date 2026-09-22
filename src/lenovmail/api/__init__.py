# Lenovmail — authored by satuapps (satuapps.com)
"""HTTP layer: FastAPI application, authentication, and routes."""

from __future__ import annotations

__all__ = ["create_app"]


def create_app():  # pragma: no cover - thin re-export for `uvicorn --factory`
    from .app import create_app as factory

    return factory()
