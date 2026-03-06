"""Tests for BotStateSnapshot model and DatabaseManager state operations."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.database.manager import DatabaseManager
from bot.database.models import Base, BotStateSnapshot


@pytest_asyncio.fixture
async def db():
    """Create an in-memory SQLite async DB for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    manager = DatabaseManager.__new__(DatabaseManager)
    manager._engine = engine
    manager._session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    manager.database_url = "sqlite+aiosqlite:///:memory:"

    yield manager

    await engine.dispose()


@pytest.mark.asyncio
async def test_save_and_load(db: DatabaseManager):
    snapshot = BotStateSnapshot(
        bot_name="bot1",
        bot_state="running",
        grid_state='{"orders":{}}',
        dca_state=None,
        risk_state=None,
        trend_state=None,
        saved_at=datetime.now(timezone.utc),
    )
    saved = await db.save_state_snapshot(snapshot)
    assert saved.id is not None

    loaded = await db.load_state_snapshot("bot1")
    assert loaded is not None
    assert loaded.bot_name == "bot1"
    assert loaded.grid_state == '{"orders":{}}'


@pytest.mark.asyncio
async def test_upsert(db: DatabaseManager):
    """Two saves with same bot_name should result in one row (update)."""
    snap1 = BotStateSnapshot(
        bot_name="bot1",
        bot_state="running",
        grid_state="v1",
        saved_at=datetime.now(timezone.utc),
    )
    await db.save_state_snapshot(snap1)

    snap2 = BotStateSnapshot(
        bot_name="bot1",
        bot_state="stopped",
        grid_state="v2",
        saved_at=datetime.now(timezone.utc),
    )
    await db.save_state_snapshot(snap2)

    loaded = await db.load_state_snapshot("bot1")
    assert loaded is not None
    assert loaded.grid_state == "v2"
    assert loaded.bot_state == "stopped"


@pytest.mark.asyncio
async def test_delete(db: DatabaseManager):
    snap = BotStateSnapshot(
        bot_name="bot_del",
        bot_state="running",
        saved_at=datetime.now(timezone.utc),
    )
    await db.save_state_snapshot(snap)
    assert await db.delete_state_snapshot("bot_del") is True

    loaded = await db.load_state_snapshot("bot_del")
    assert loaded is None


@pytest.mark.asyncio
async def test_delete_nonexistent(db: DatabaseManager):
    assert await db.delete_state_snapshot("no_such_bot") is False


@pytest.mark.asyncio
async def test_load_nonexistent(db: DatabaseManager):
    loaded = await db.load_state_snapshot("no_such_bot")
    assert loaded is None


@pytest.mark.asyncio
async def test_multiple_bots(db: DatabaseManager):
    for name in ["bot_a", "bot_b", "bot_c"]:
        snap = BotStateSnapshot(
            bot_name=name,
            bot_state="running",
            grid_state=f"state_{name}",
            saved_at=datetime.now(timezone.utc),
        )
        await db.save_state_snapshot(snap)

    a = await db.load_state_snapshot("bot_a")
    c = await db.load_state_snapshot("bot_c")
    assert a.grid_state == "state_bot_a"
    assert c.grid_state == "state_bot_c"
