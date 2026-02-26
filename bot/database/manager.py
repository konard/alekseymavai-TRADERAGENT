"""
Database Manager with async operations and connection pooling.
Provides high-level interface for database operations.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.database.models import (
    Base,
    Bot,
    BotLog,
    BotStateSnapshot,
    DCAHistory,
    ExchangeCredential,
    GridLevel,
    Order,
    Trade,
)
from bot.utils.logger import LoggerMixin

T = TypeVar("T", bound=Base)


class DatabaseManager(LoggerMixin):
    """
    Async database manager with connection pooling.

    Features:
    - Async SQLAlchemy with asyncpg
    - Connection pooling
    - Context managers for sessions
    - High-level CRUD operations
    - Transaction support
    """

    def __init__(
        self,
        database_url: str,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_pre_ping: bool = True,
        echo: bool = False,
    ) -> None:
        """
        Initialize Database Manager.

        Args:
            database_url: PostgreSQL connection URL (postgresql+asyncpg://...)
            pool_size: Number of connections to maintain
            max_overflow: Maximum overflow connections beyond pool_size
            pool_pre_ping: Enable connection health checks
            echo: Whether to log all SQL statements
        """
        self.database_url = database_url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_pre_ping = pool_pre_ping
        self._echo = echo

        self.logger.info(
            "Initializing DatabaseManager",
            pool_size=pool_size,
            max_overflow=max_overflow,
        )

    async def initialize(self) -> None:
        """Initialize database engine and session factory"""
        try:
            # Create async engine with connection pool
            self._engine = create_async_engine(
                self.database_url,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                pool_pre_ping=self._pool_pre_ping,
                echo=self._echo,
            )

            # Create session factory
            self._session_factory = async_sessionmaker(
                self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            self.logger.info("Database engine initialized successfully")

        except Exception as e:
            self.logger.error("Failed to initialize database", error=str(e))
            raise

    async def close(self) -> None:
        """Close database connections"""
        if self._engine:
            await self._engine.dispose()
            self.logger.info("Database connections closed")

    async def health_check(self) -> bool:
        """
        Perform a health check on the database connection.

        Returns:
            True if database is accessible, False otherwise
        """
        try:
            async with self.session() as session:
                await session.execute(select(1))
            return True
        except Exception as e:
            self.logger.error("database_health_check_failed", error=str(e))
            return False

    async def create_all_tables(self) -> None:
        """Create all tables in the database"""
        if not self._engine:
            raise RuntimeError("Database not initialized")

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            self.logger.info("All database tables created")

    async def drop_all_tables(self) -> None:
        """Drop all tables from the database (use with caution!)"""
        if not self._engine:
            raise RuntimeError("Database not initialized")

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            self.logger.warning("All database tables dropped")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Context manager for database sessions.

        Usage:
            async with db.session() as session:
                # Use session
                pass
        """
        if not self._session_factory:
            raise RuntimeError("Database not initialized")

        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # CRUD Operations - Generic

    async def create(self, obj: T) -> T:
        """Create a new database object"""
        async with self.session() as session:
            session.add(obj)
            await session.flush()
            await session.refresh(obj)
            return obj

    async def get(self, model: type[T], id: int) -> T | None:
        """Get object by ID"""
        async with self.session() as session:
            result = await session.execute(select(model).where(model.id == id))
            return result.scalar_one_or_none()

    async def update(self, obj: T) -> T:
        """Update an existing object"""
        async with self.session() as session:
            merged = await session.merge(obj)
            await session.flush()
            await session.refresh(merged)
            return merged

    async def delete(self, obj: T) -> None:
        """Delete an object"""
        async with self.session() as session:
            await session.delete(obj)

    # Bot Operations

    async def create_bot(self, bot: Bot) -> Bot:
        """Create a new bot"""
        self.logger.info("Creating bot", name=bot.name, symbol=bot.symbol)
        return await self.create(bot)

    async def get_bot(self, bot_id: int) -> Bot | None:
        """Get bot by ID"""
        return await self.get(Bot, bot_id)

    async def get_bot_by_name(self, name: str) -> Bot | None:
        """Get bot by name"""
        async with self.session() as session:
            result = await session.execute(select(Bot).where(Bot.name == name))
            return result.scalar_one_or_none()

    async def get_all_bots(self, status: str | None = None) -> list[Bot]:
        """Get all bots, optionally filtered by status"""
        async with self.session() as session:
            query = select(Bot)
            if status:
                query = query.where(Bot.status == status)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def update_bot(self, bot: Bot) -> Bot:
        """Update bot"""
        self.logger.info("Updating bot", bot_id=bot.id, name=bot.name)
        return await self.update(bot)

    # Exchange Credentials Operations

    async def create_credentials(self, credentials: ExchangeCredential) -> ExchangeCredential:
        """Create new exchange credentials"""
        self.logger.info(
            "Creating credentials",
            name=credentials.name,
            exchange=credentials.exchange_id,
        )
        return await self.create(credentials)

    async def get_credentials(self, credentials_id: int) -> ExchangeCredential | None:
        """Get credentials by ID"""
        return await self.get(ExchangeCredential, credentials_id)

    async def get_credentials_by_name(self, name: str) -> ExchangeCredential | None:
        """Get credentials by name"""
        async with self.session() as session:
            result = await session.execute(
                select(ExchangeCredential).where(ExchangeCredential.name == name)
            )
            return result.scalar_one_or_none()

    # Order Operations

    async def create_order(self, order: Order) -> Order:
        """Create a new order"""
        self.logger.info(
            "Creating order",
            bot_id=order.bot_id,
            symbol=order.symbol,
            side=order.side,
            amount=order.amount,
        )
        return await self.create(order)

    async def get_order(self, order_id: int) -> Order | None:
        """Get order by ID"""
        return await self.get(Order, order_id)

    async def get_order_by_exchange_id(self, exchange_order_id: str) -> Order | None:
        """Get order by exchange order ID"""
        async with self.session() as session:
            result = await session.execute(
                select(Order).where(Order.exchange_order_id == exchange_order_id)
            )
            return result.scalar_one_or_none()

    async def get_bot_orders(self, bot_id: int, status: str | None = None) -> list[Order]:
        """Get all orders for a bot, optionally filtered by status"""
        async with self.session() as session:
            query = select(Order).where(Order.bot_id == bot_id)
            if status:
                query = query.where(Order.status == status)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def update_order(self, order: Order) -> Order:
        """Update order"""
        return await self.update(order)

    # Trade Operations

    async def create_trade(self, trade: Trade) -> Trade:
        """Create a new trade record"""
        self.logger.info(
            "Creating trade",
            bot_id=trade.bot_id,
            symbol=trade.symbol,
            side=trade.side,
            amount=trade.amount,
        )
        return await self.create(trade)

    async def get_bot_trades(
        self,
        bot_id: int,
        limit: int | None = None,
    ) -> list[Trade]:
        """Get trades for a bot, optionally limited"""
        async with self.session() as session:
            query = select(Trade).where(Trade.bot_id == bot_id).order_by(Trade.executed_at.desc())
            if limit:
                query = query.limit(limit)
            result = await session.execute(query)
            return list(result.scalars().all())

    # Grid Level Operations

    async def create_grid_level(self, grid_level: GridLevel) -> GridLevel:
        """Create a grid level"""
        return await self.create(grid_level)

    async def get_bot_grid_levels(self, bot_id: int, active_only: bool = True) -> list[GridLevel]:
        """Get grid levels for a bot"""
        async with self.session() as session:
            query = select(GridLevel).where(GridLevel.bot_id == bot_id)
            if active_only:
                query = query.where(GridLevel.is_active.is_(True))
            query = query.order_by(GridLevel.level)
            result = await session.execute(query)
            return list(result.scalars().all())

    async def update_grid_level(self, grid_level: GridLevel) -> GridLevel:
        """Update grid level"""
        return await self.update(grid_level)

    # DCA History Operations

    async def create_dca_history(self, dca_history: DCAHistory) -> DCAHistory:
        """Create DCA history entry"""
        self.logger.info(
            "Creating DCA history",
            bot_id=dca_history.bot_id,
            step=dca_history.dca_step,
            price=dca_history.buy_price,
        )
        return await self.create(dca_history)

    async def get_bot_dca_history(
        self,
        bot_id: int,
        limit: int | None = None,
    ) -> list[DCAHistory]:
        """Get DCA history for a bot"""
        async with self.session() as session:
            query = (
                select(DCAHistory)
                .where(DCAHistory.bot_id == bot_id)
                .order_by(DCAHistory.executed_at.desc())
            )
            if limit:
                query = query.limit(limit)
            result = await session.execute(query)
            return list(result.scalars().all())

    # Bot Log Operations

    async def create_log(self, log: BotLog) -> BotLog:
        """Create a bot log entry"""
        return await self.create(log)

    async def get_bot_logs(
        self,
        bot_id: int,
        level: str | None = None,
        limit: int | None = 100,
    ) -> list[BotLog]:
        """Get logs for a bot"""
        async with self.session() as session:
            query = select(BotLog).where(BotLog.bot_id == bot_id)
            if level:
                query = query.where(BotLog.level == level)
            query = query.order_by(BotLog.created_at.desc())
            if limit:
                query = query.limit(limit)
            result = await session.execute(query)
            return list(result.scalars().all())

    # State Snapshot Operations

    async def save_state_snapshot(self, snapshot: BotStateSnapshot) -> BotStateSnapshot:
        """Upsert a bot state snapshot (insert or update by bot_name)."""
        async with self.session() as session:
            result = await session.execute(
                select(BotStateSnapshot).where(BotStateSnapshot.bot_name == snapshot.bot_name)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.bot_state = snapshot.bot_state
                existing.grid_state = snapshot.grid_state
                existing.dca_state = snapshot.dca_state
                existing.risk_state = snapshot.risk_state
                existing.trend_state = snapshot.trend_state
                existing.hybrid_state = snapshot.hybrid_state
                existing.saved_at = snapshot.saved_at
                await session.flush()
                await session.refresh(existing)
                return existing
            else:
                session.add(snapshot)
                await session.flush()
                await session.refresh(snapshot)
                return snapshot

    async def load_state_snapshot(self, bot_name: str) -> BotStateSnapshot | None:
        """Load a bot state snapshot by bot_name."""
        async with self.session() as session:
            result = await session.execute(
                select(BotStateSnapshot).where(BotStateSnapshot.bot_name == bot_name)
            )
            return result.scalar_one_or_none()

    async def delete_state_snapshot(self, bot_name: str) -> bool:
        """Delete a bot state snapshot by bot_name. Returns True if deleted."""
        async with self.session() as session:
            result = await session.execute(
                select(BotStateSnapshot).where(BotStateSnapshot.bot_name == bot_name)
            )
            snapshot = result.scalar_one_or_none()
            if snapshot:
                await session.delete(snapshot)
                return True
            return False

    # Statistics and Analytics

    async def get_bot_statistics(self, bot_id: int) -> dict[str, Any]:
        """Get comprehensive statistics for a bot"""
        async with self.session():
            bot = await self.get_bot(bot_id)
            if not bot:
                return {}

            trades = await self.get_bot_trades(bot_id)
            open_orders = await self.get_bot_orders(bot_id, status="open")

            return {
                "bot_id": bot_id,
                "total_invested": float(bot.total_invested),
                "current_profit": float(bot.current_profit),
                "total_trades": bot.total_trades,
                "open_orders": len(open_orders),
                "status": bot.status,
                "recent_trades": len(trades),
            }
