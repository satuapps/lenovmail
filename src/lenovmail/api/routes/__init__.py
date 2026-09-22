# Lenovmail — authored by satuapps (satuapps.com)
"""Lenovmail HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter

from . import accounts, agent, auth, events, mail, system

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(mail.router)
api_router.include_router(agent.router)
api_router.include_router(events.router)

__all__ = ["api_router"]
