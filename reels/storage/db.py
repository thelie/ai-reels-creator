"""Хранилище проектов (SQLAlchemy async; SQLite по умолчанию, PostgreSQL — через REELS_DATABASE_URL)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(32), default="collecting")
    duration: Mapped[float] = mapped_column(Float, default=20.0)
    duration_set: Mapped[bool] = mapped_column(Boolean, default=False)  # пользователь выбрал длительность сам
    template_id: Mapped[str] = mapped_column(String(64), default="auto")
    script: Mapped[str] = mapped_column(Text, default="")
    # Результаты анализа и планирования
    analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"assets": [...], "hook": {...}}
    reference: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    template: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # итоговый StyleTemplate
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # текущий PlanDraft
    edl_version: Mapped[int] = mapped_column(Integer, default=0)
    variant_seed: Mapped[int] = mapped_column(Integer, default=0)
    status_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    assets: Mapped[list[Asset]] = relationship(back_populates="project", order_by="Asset.created_at",
                                               lazy="selectin", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # video | photo | audio | reference
    path: Mapped[str] = mapped_column(Text)
    orig_name: Mapped[str] = mapped_column(Text, default="")
    tg_file_unique_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship(back_populates="assets")


class EdlVersion(Base):
    __tablename__ = "edl_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    edl: Mapped[dict] = mapped_column(JSON)
    plan: Mapped[dict] = mapped_column(JSON)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    template: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Render(Base):
    __tablename__ = "renders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    edl_version: Mapped[int] = mapped_column(Integer)
    quality: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(Text)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)
    qa: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def get_or_create_user(self, s: AsyncSession, tg_id: int) -> User:
        user = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if user is None:
            user = User(tg_id=tg_id, settings={})
            s.add(user)
            await s.flush()
        return user
