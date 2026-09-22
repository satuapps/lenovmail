# Lenovmail — authored by satuapps
"""Application user model (not a mail account)."""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Timestamped, UUIDPk


class User(UUIDPk, Timestamped, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role in ('admin','user')", name="users_role_check"),)

    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
