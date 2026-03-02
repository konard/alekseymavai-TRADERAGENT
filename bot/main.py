"""
Main entry point for TRADERAGENT bot.
Initializes and runs orchestrators, Telegram bot, and monitoring stack.
"""

import asyncio
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from bot.api.bybit_direct_client import ByBitDirectClient
from bot.api.exchange_client import ExchangeAPIClient
from bot.config.manager import ConfigManager
from bot.database.manager import DatabaseManager
from bot.monitoring.alert_handler import Alert, AlertHandler
from bot.monitoring.metrics_collector import MetricsCollector
from bot.monitoring.metrics_exporter import MetricsExporter
from bot.orchestrator.bot_orchestrator import BotOrchestrator
from bot.telegram.bot import TelegramBot
from bot.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


class BotApplication:
    """Main bot application manager."""

    def __init__(self):
        """Initialize bot application."""
        self.config_manager: ConfigManager | None = None
        self.db_manager: DatabaseManager | None = None
        self.orchestrators: dict[str, BotOrchestrator] = {}
        self.telegram_bot: TelegramBot | None = None
        self.metrics_exporter: MetricsExporter | None = None
        self.metrics_collector: MetricsCollector | None = None
        self.alert_handler: AlertHandler | None = None
        self._alert_server_runner: web.AppRunner | None = None
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self.running = False

        # Multi-pair / auto-trade components (set in initialize() if enabled)
        self._scanner_task: asyncio.Task | None = None
        self._portfolio_risk_manager = None  # PortfolioRiskManager
        self._pair_template_manager = None   # PairTemplateManager
        self._scanner = None                  # MarketScanner
        self._main_config = None              # cached AppConfig
        self._redis_url: str = "redis://localhost:6379"
        self._main_exchange_client = None     # shared exchange client for scanner

    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("initializing_bot_application")

        # Load configuration
        config_path = os.getenv("CONFIG_PATH", "configs/production.yaml")
        logger.info("loading_config", path=config_path)

        self.config_manager = ConfigManager(Path(config_path))
        main_config = self.config_manager.load()

        # Setup logging
        log_level = os.getenv("LOG_LEVEL", main_config.log_level)
        setup_logging(log_level=log_level)

        # Initialize database
        database_url = os.getenv("DATABASE_URL", main_config.database_url)
        logger.info("initializing_database")
        self.db_manager = DatabaseManager(database_url)
        await self.db_manager.initialize()

        # Redis URL
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

        # Initialize orchestrators for each bot config
        logger.info("initializing_orchestrators", bot_count=len(main_config.bots))
        for bot_config in main_config.bots:
            logger.info("initializing_bot", bot_name=bot_config.name)

            # Load API credentials from database
            credentials = await self.db_manager.get_credentials_by_name(
                bot_config.exchange.credentials_name
            )
            if not credentials:
                logger.error(
                    "credentials_not_found",
                    bot_name=bot_config.name,
                    credentials_name=bot_config.exchange.credentials_name,
                )
                continue

            # Decrypt credentials
            encryption_key = os.getenv("ENCRYPTION_KEY", main_config.encryption_key)
            from cryptography.fernet import Fernet

            fernet = Fernet(encryption_key.encode())
            api_key = fernet.decrypt(credentials.api_key_encrypted.encode()).decode()
            api_secret = fernet.decrypt(credentials.api_secret_encrypted.encode()).decode()
            password = None
            if credentials.password_encrypted:
                password = fernet.decrypt(credentials.password_encrypted.encode()).decode()

            # Create exchange client
            # For Bybit Demo Trading (sandbox=true), use ByBitDirectClient
            # because CCXT sandbox mode routes to testnet.bybit.com, NOT
            # api-demo.bybit.com which is needed for demo trading
            if bot_config.exchange.exchange_id == "bybit" and bot_config.exchange.sandbox:
                logger.info(
                    "using_bybit_direct_client",
                    bot_name=bot_config.name,
                    mode="demo_trading",
                    url="api-demo.bybit.com",
                )
                exchange_client = ByBitDirectClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    testnet=True,
                    market_type="linear",
                )
            else:
                exchange_client = ExchangeAPIClient(
                    exchange_id=bot_config.exchange.exchange_id,
                    api_key=api_key,
                    api_secret=api_secret,
                    password=password,
                    sandbox=bot_config.exchange.sandbox,
                )
            await exchange_client.initialize()

            # Create orchestrator — share portfolio RM if available (multi-pair)
            orchestrator = BotOrchestrator(
                bot_config=bot_config,
                exchange_client=exchange_client,
                db_manager=self.db_manager,
                redis_url=redis_url,
                portfolio_risk_manager=self._portfolio_risk_manager,
            )
            await orchestrator.initialize()

            self.orchestrators[bot_config.name] = orchestrator

            # Auto-start if configured
            if bot_config.auto_start:
                logger.info("auto_starting_bot", bot_name=bot_config.name)
                await orchestrator.start()

        # Initialize monitoring stack
        metrics_port = int(os.getenv("METRICS_PORT", "9100"))
        alerts_port = int(os.getenv("ALERTS_PORT", "8080"))

        logger.info("initializing_monitoring", metrics_port=metrics_port, alerts_port=alerts_port)
        self.metrics_exporter = MetricsExporter(port=metrics_port)
        self.metrics_collector = MetricsCollector(
            exporter=self.metrics_exporter,
            orchestrators=self.orchestrators,
        )
        self.alert_handler = AlertHandler()

        # Initialize Telegram bot if configured
        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")

        if telegram_token and telegram_chat_ids:
            logger.info("initializing_telegram_bot")
            allowed_chat_ids = [
                int(chat_id.strip()) for chat_id in telegram_chat_ids.split(",") if chat_id.strip()
            ]

            self.telegram_bot = TelegramBot(
                token=telegram_token,
                allowed_chat_ids=allowed_chat_ids,
                orchestrators=self.orchestrators,
                redis_url=redis_url,
            )

            # Bridge: AlertManager → AlertHandler → Telegram
            self._setup_alert_telegram_bridge(allowed_chat_ids)
        else:
            logger.warning("telegram_bot_not_configured")

        # Cache config and redis URL for dynamic bot lifecycle
        self._main_config = main_config
        self._redis_url = redis_url

        # Initialize portfolio risk manager
        try:
            from bot.core.portfolio_risk_manager import PortfolioRiskManager
            from decimal import Decimal
            # Use total capital as sum of all initial bot allocations (10k default)
            total_cap = Decimal("10000")
            self._portfolio_risk_manager = PortfolioRiskManager(total_capital=total_cap)
        except Exception as e:
            logger.warning("portfolio_risk_manager_init_failed", error=str(e))

        # Initialize auto-trade scanner if enabled
        if main_config.auto_trade.enabled:
            try:
                from bot.config.pair_template import PairTemplateManager
                from bot.orchestrator.market_scanner import MarketScanner

                self._pair_template_manager = PairTemplateManager()

                # Use the first exchange client as the scanner client
                if self.orchestrators:
                    first_orch = next(iter(self.orchestrators.values()))
                    self._main_exchange_client = first_orch._exchange_client
                    self._scanner = MarketScanner(
                        exchange_client=self._main_exchange_client,
                        config=main_config.auto_trade.scanner,
                    )
                    self._scanner_task = asyncio.create_task(self._scanner_loop())
                    logger.info("auto_trade_scanner_started", max_bots=main_config.auto_trade.max_bots)
            except Exception as e:
                logger.warning("auto_trade_scanner_init_failed", error=str(e))

        logger.info("bot_application_initialized")

    # ------------------------------------------------------------------
    # Dynamic bot lifecycle (Multi-Pair / Auto-Trade)
    # ------------------------------------------------------------------

    async def add_bot(self, bot_config) -> BotOrchestrator | None:
        """
        Dynamically create, initialise, and start a new BotOrchestrator.

        Args:
            bot_config: A BotConfig instance for the new bot.

        Returns:
            The started BotOrchestrator, or None if initialisation failed.
        """
        bot_name = bot_config.name
        if bot_name in self.orchestrators:
            logger.warning("add_bot_duplicate", bot_name=bot_name)
            return self.orchestrators[bot_name]

        try:
            credentials = await self.db_manager.get_credentials_by_name(
                bot_config.exchange.credentials_name
            )
            if not credentials:
                logger.error(
                    "add_bot_credentials_not_found",
                    bot_name=bot_name,
                    credentials_name=bot_config.exchange.credentials_name,
                )
                return None

            from cryptography.fernet import Fernet

            encryption_key = os.getenv(
                "ENCRYPTION_KEY",
                self._main_config.encryption_key if self._main_config else "",
            )
            fernet = Fernet(encryption_key.encode())
            api_key = fernet.decrypt(credentials.api_key_encrypted.encode()).decode()
            api_secret = fernet.decrypt(credentials.api_secret_encrypted.encode()).decode()
            password = None
            if credentials.password_encrypted:
                password = fernet.decrypt(credentials.password_encrypted.encode()).decode()

            if bot_config.exchange.exchange_id == "bybit" and bot_config.exchange.sandbox:
                exchange_client = ByBitDirectClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    testnet=True,
                    market_type="linear",
                )
            else:
                exchange_client = ExchangeAPIClient(
                    exchange_id=bot_config.exchange.exchange_id,
                    api_key=api_key,
                    api_secret=api_secret,
                    password=password,
                    sandbox=bot_config.exchange.sandbox,
                )
            await exchange_client.initialize()

            orchestrator = BotOrchestrator(
                bot_config=bot_config,
                exchange_client=exchange_client,
                db_manager=self.db_manager,
                redis_url=self._redis_url,
                portfolio_risk_manager=self._portfolio_risk_manager,
            )
            await orchestrator.initialize()
            await orchestrator.start()

            self.orchestrators[bot_name] = orchestrator

            # Update metrics collector to include new bot
            if self.metrics_collector:
                self.metrics_collector.orchestrators = self.orchestrators

            # Notify Telegram
            if self.telegram_bot:
                try:
                    await self.telegram_bot._broadcast(
                        f"Bot started: {bot_name} ({bot_config.symbol})"
                    )
                except Exception:
                    pass

            logger.info("bot_added", bot_name=bot_name, symbol=bot_config.symbol)
            return orchestrator

        except Exception as e:
            logger.error("add_bot_failed", bot_name=bot_name, error=str(e), exc_info=True)
            return None

    async def remove_bot(self, bot_name: str) -> None:
        """
        Gracefully stop and remove a BotOrchestrator.

        Args:
            bot_name: Name of the bot to remove.
        """
        orchestrator = self.orchestrators.pop(bot_name, None)
        if orchestrator is None:
            logger.warning("remove_bot_not_found", bot_name=bot_name)
            return

        try:
            await orchestrator.stop()
            await orchestrator.cleanup()
        except Exception as e:
            logger.error("remove_bot_stop_failed", bot_name=bot_name, error=str(e))

        # Release portfolio risk manager allocation
        if self._portfolio_risk_manager:
            try:
                self._portfolio_risk_manager.release_allocation(bot_name, amount=0)
            except Exception:
                pass

        # Update metrics collector
        if self.metrics_collector:
            self.metrics_collector.orchestrators = self.orchestrators

        # Notify Telegram
        if self.telegram_bot:
            try:
                await self.telegram_bot._broadcast(f"Bot removed: {bot_name}")
            except Exception:
                pass

        logger.info("bot_removed", bot_name=bot_name)

    async def _scanner_loop(self) -> None:
        """
        Background task that periodically scans the market for top pairs
        and adds/removes bots accordingly.

        Runs every auto_trade.scanner.interval_minutes minutes.
        """
        if not self._main_config or not self._scanner:
            return

        auto_cfg = self._main_config.auto_trade
        interval_secs = auto_cfg.scanner.interval_minutes * 60

        logger.info(
            "scanner_loop_started",
            interval_minutes=auto_cfg.scanner.interval_minutes,
            max_bots=auto_cfg.max_bots,
        )

        while self.running:
            try:
                await asyncio.sleep(interval_secs)
                if not self.running:
                    break

                logger.info("scanner_loop_scanning")
                scan_results = await self._scanner.scan()

                # Determine which symbols to trade (top-N by confidence)
                top = [
                    r for r in scan_results
                    if r.confidence >= auto_cfg.min_confidence
                ][: auto_cfg.max_bots]

                top_symbols = {r.symbol for r in top}

                # Remove bots whose symbol is no longer in top
                auto_bot_names = [
                    name for name in list(self.orchestrators.keys())
                    if name.startswith("auto_")
                ]
                for bot_name in auto_bot_names:
                    orch = self.orchestrators.get(bot_name)
                    if orch and hasattr(orch, "bot_config"):
                        if orch.bot_config.symbol not in top_symbols:
                            logger.info("scanner_removing_bot", bot_name=bot_name)
                            await self.remove_bot(bot_name)

                # Add bots for new top symbols
                current_count = len(self.orchestrators)
                for scan_result in top:
                    if current_count >= auto_cfg.max_bots:
                        break
                    symbol = scan_result.symbol
                    expected_name = f"auto_{symbol.replace('/', '_')}_{auto_cfg.strategy_template}"
                    if expected_name in self.orchestrators:
                        continue
                    if not self._pair_template_manager or not self._main_config.bots:
                        continue
                    try:
                        base_cfg = self._main_config.bots[0]
                        new_cfg = await self._pair_template_manager.create_config(
                            symbol=symbol,
                            strategy=auto_cfg.strategy_template,
                            exchange_client=self._main_exchange_client,
                            base_config=base_cfg,
                        )
                        added = await self.add_bot(new_cfg)
                        if added:
                            current_count += 1
                    except Exception as e:
                        logger.error("scanner_add_bot_failed", symbol=symbol, error=str(e))

            except asyncio.CancelledError:
                logger.info("scanner_loop_cancelled")
                break
            except Exception as e:
                logger.error("scanner_loop_error", error=str(e), exc_info=True)

        logger.info("scanner_loop_stopped")

    def _setup_alert_telegram_bridge(self, allowed_chat_ids: list[int]) -> None:
        """Register AlertHandler callback that forwards alerts to Telegram."""
        telegram_bot = self.telegram_bot

        async def send_alert_to_telegram(alert: Alert) -> None:
            if not telegram_bot:
                return
            message = alert.format_message()
            for chat_id in allowed_chat_ids:
                try:
                    await telegram_bot.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                    )
                except Exception as e:
                    logger.error(
                        "alert_telegram_send_failed",
                        chat_id=chat_id,
                        error=str(e),
                    )

        self.alert_handler.add_callback(send_alert_to_telegram)
        logger.info("alert_telegram_bridge_configured")

    async def _start_alert_server(self, port: int = 8080) -> None:
        """Start HTTP server for AlertManager webhooks."""
        app = web.Application()
        app.router.add_routes(self.alert_handler.routes)

        self._alert_server_runner = web.AppRunner(app)
        await self._alert_server_runner.setup()
        site = web.TCPSite(self._alert_server_runner, "0.0.0.0", port)
        await site.start()
        logger.info("alert_server_started", port=port)

    async def start(self) -> None:
        """Start the bot application with all components concurrently."""
        logger.info("starting_bot_application")
        self.running = True
        self._shutdown_event.clear()

        try:
            # Start metrics HTTP server (port 9100)
            if self.metrics_exporter:
                await self.metrics_exporter.start()
                logger.info("metrics_exporter_started")

            # Start metrics collection loop
            if self.metrics_collector:
                await self.metrics_collector.start()
                logger.info("metrics_collector_started")

            # Start alert webhook server (port 8080)
            if self.alert_handler:
                alerts_port = int(os.getenv("ALERTS_PORT", "8080"))
                await self._start_alert_server(port=alerts_port)

            # Start Telegram bot (non-blocking) or wait for shutdown
            if self.telegram_bot:
                logger.info("starting_telegram_bot")
                await self.telegram_bot.start()
            else:
                # No Telegram — wait for shutdown signal
                await self._shutdown_event.wait()

        except asyncio.CancelledError:
            logger.info("bot_application_cancelled")
        except Exception as e:
            logger.error("bot_application_error", error=str(e), exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop the bot application."""
        logger.info("stopping_bot_application")
        self.running = False
        self._shutdown_event.set()

        # Cancel scanner loop
        if self._scanner_task and not self._scanner_task.done():
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except asyncio.CancelledError:
                pass

        # Stop metrics collector
        if self.metrics_collector:
            logger.info("stopping_metrics_collector")
            try:
                await self.metrics_collector.stop()
            except Exception as e:
                logger.error("metrics_collector_stop_failed", error=str(e))

        # Stop metrics exporter
        if self.metrics_exporter:
            logger.info("stopping_metrics_exporter")
            try:
                await self.metrics_exporter.stop()
            except Exception as e:
                logger.error("metrics_exporter_stop_failed", error=str(e))

        # Stop alert server
        if self._alert_server_runner:
            logger.info("stopping_alert_server")
            try:
                await self._alert_server_runner.cleanup()
                self._alert_server_runner = None
            except Exception as e:
                logger.error("alert_server_stop_failed", error=str(e))

        # Stop all orchestrators
        for name, orchestrator in self.orchestrators.items():
            logger.info("stopping_orchestrator", bot_name=name)
            try:
                await orchestrator.stop()
                await orchestrator.cleanup()
            except Exception as e:
                logger.error(
                    "orchestrator_stop_failed",
                    bot_name=name,
                    error=str(e),
                )

        # Stop Telegram bot
        if self.telegram_bot:
            logger.info("stopping_telegram_bot")
            try:
                await self.telegram_bot.stop()
            except Exception as e:
                logger.error("telegram_bot_stop_failed", error=str(e))

        # Close database
        if self.db_manager:
            logger.info("closing_database")
            await self.db_manager.close()

        logger.info("bot_application_stopped")

    async def cleanup(self) -> None:
        """Cleanup resources."""
        await self.stop()


async def main() -> None:
    """Main entry point."""
    app = BotApplication()

    # Setup signal handlers
    def signal_handler(sig, frame):
        logger.info("signal_received", signal=sig)
        asyncio.create_task(app.stop())

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Initialize application
        await app.initialize()

        # Start application
        await app.start()

    except KeyboardInterrupt:
        logger.info("keyboard_interrupt_received")
    except Exception as e:
        logger.error("application_error", error=str(e), exc_info=True)
        sys.exit(1)
    finally:
        await app.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
