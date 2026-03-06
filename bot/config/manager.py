"""
Configuration Manager with YAML support, Pydantic validation, and hot reload.
"""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from bot.config.schemas import AppConfig, BotConfig
from bot.utils.logger import LoggerMixin


class ConfigManager(LoggerMixin):
    """
    Configuration manager with YAML loading, validation, and hot reload.

    Features:
    - Load and validate YAML configurations with Pydantic
    - Hot reload on configuration file changes
    - Configuration versioning with hash tracking
    - Environment variable substitution
    - Multiple bot configuration management
    """

    def __init__(self, config_path: Path) -> None:
        """
        Initialize ConfigManager.

        Args:
            config_path: Path to main configuration file
        """
        self.config_path = config_path
        self._config: AppConfig | None = None
        self._config_hash: str | None = None
        self._reload_callbacks: list[Callable[[AppConfig], None]] = []
        self._observer: Any = None
        self._watch_enabled = False

        self.logger.info("Initializing ConfigManager", path=str(config_path))

    def load(self) -> AppConfig:
        """
        Load and validate configuration from file.

        Returns:
            Validated AppConfig instance

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValidationError: If config validation fails
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        try:
            # Read YAML file
            with open(self.config_path, encoding="utf-8") as f:
                raw_config = yaml.safe_load(f)

            # Calculate config hash for versioning
            config_str = json.dumps(raw_config, sort_keys=True)
            new_hash = hashlib.sha256(config_str.encode()).hexdigest()[:16]

            # Validate with Pydantic
            config = AppConfig(**raw_config)

            # Update state
            self._config = config
            self._config_hash = new_hash

            self.logger.info(
                "Configuration loaded successfully",
                version_hash=new_hash,
                bots_count=len(config.bots),
            )

            return config

        except yaml.YAMLError as e:
            self.logger.error("Failed to parse YAML", error=str(e))
            raise
        except ValidationError as e:
            self.logger.error("Configuration validation failed", error=str(e))
            raise
        except Exception as e:
            self.logger.error("Failed to load configuration", error=str(e))
            raise

    def reload(self) -> AppConfig:
        """
        Reload configuration from file.

        Returns:
            Updated AppConfig instance
        """
        self.logger.info("Reloading configuration")

        old_hash = self._config_hash
        config = self.load()

        if old_hash != self._config_hash:
            self.logger.info(
                "Configuration changed",
                old_hash=old_hash,
                new_hash=self._config_hash,
            )

            # Trigger callbacks
            for callback in self._reload_callbacks:
                try:
                    callback(config)
                except Exception as e:
                    self.logger.error(
                        "Error in reload callback",
                        callback=callback.__name__,
                        error=str(e),
                    )
        else:
            self.logger.debug("Configuration unchanged")

        return config

    def get_config(self) -> AppConfig:
        """
        Get current configuration.

        Returns:
            Current AppConfig instance

        Raises:
            RuntimeError: If config not loaded
        """
        if self._config is None:
            raise RuntimeError("Configuration not loaded. Call load() first.")
        return self._config

    def get_bot_config(self, bot_name: str) -> BotConfig | None:
        """
        Get configuration for a specific bot.

        Args:
            bot_name: Name of the bot

        Returns:
            BotConfig if found, None otherwise
        """
        config = self.get_config()
        for bot_config in config.bots:
            if bot_config.name == bot_name:
                return bot_config
        return None

    def get_config_version(self) -> str:
        """
        Get current configuration version hash.

        Returns:
            Configuration version hash
        """
        if self._config_hash is None:
            raise RuntimeError("Configuration not loaded")
        return self._config_hash

    def register_reload_callback(self, callback: Callable[[AppConfig], None]) -> None:
        """
        Register a callback to be called when configuration is reloaded.

        Args:
            callback: Function to call with new config
        """
        self._reload_callbacks.append(callback)
        self.logger.info("Registered reload callback", callback=callback.__name__)

    def enable_watch(self) -> None:
        """Enable file watching for hot reload"""
        if self._watch_enabled:
            self.logger.warning("File watching already enabled")
            return

        class ConfigFileHandler(FileSystemEventHandler):
            """Handler for configuration file changes"""

            def __init__(self, manager: ConfigManager) -> None:
                self.manager = manager

            def on_modified(self, event: FileSystemEvent) -> None:
                if event.src_path == str(self.manager.config_path):
                    self.manager.logger.info("Configuration file modified, reloading...")
                    try:
                        self.manager.reload()
                    except Exception as e:
                        self.manager.logger.error("Failed to reload configuration", error=str(e))

        # Set up file observer
        self._observer = Observer()
        event_handler = ConfigFileHandler(self)
        self._observer.schedule(
            event_handler,
            str(self.config_path.parent),
            recursive=False,
        )
        self._observer.start()
        self._watch_enabled = True

        self.logger.info(
            "File watching enabled",
            path=str(self.config_path),
        )

    def disable_watch(self) -> None:
        """Disable file watching"""
        if not self._watch_enabled:
            return

        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

        self._watch_enabled = False
        self.logger.info("File watching disabled")

    def save_config(self, config: AppConfig, path: Path | None = None) -> None:
        """
        Save configuration to file.

        Args:
            config: Configuration to save
            path: Optional path to save to (defaults to current config_path)
        """
        save_path = path or self.config_path

        try:
            # Convert to dict
            config_dict = config.model_dump(mode="json")

            # Write YAML
            with open(save_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    config_dict,
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )

            self.logger.info("Configuration saved", path=str(save_path))

        except Exception as e:
            self.logger.error("Failed to save configuration", error=str(e))
            raise

    @staticmethod
    def create_example_config(path: Path) -> None:
        """
        Create an example configuration file.

        Args:
            path: Path where to create the example config
        """
        example_config = {
            "database_url": "postgresql+asyncpg://user:password@localhost/traderagent",
            "database_pool_size": 5,
            "log_level": "INFO",
            "log_to_file": True,
            "log_to_console": True,
            "json_logs": False,
            "encryption_key": "REPLACE_WITH_BASE64_ENCODED_32_BYTE_KEY",
            "bots": [
                {
                    "version": 1,
                    "name": "example_grid_bot",
                    "symbol": "BTC/USDT",
                    "strategy": "grid",
                    "exchange": {
                        "exchange_id": "binance",
                        "credentials_name": "binance_main",
                        "sandbox": True,
                    },
                    "grid": {
                        "enabled": True,
                        "upper_price": "50000",
                        "lower_price": "40000",
                        "grid_levels": 10,
                        "amount_per_grid": "100",
                        "profit_per_grid": "0.01",
                    },
                    "risk_management": {
                        "max_position_size": "10000",
                        "stop_loss_percentage": "0.15",
                        "min_order_size": "10",
                    },
                    "notifications": {
                        "enabled": False,
                    },
                    "dry_run": True,
                    "auto_start": False,
                }
            ],
        }

        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                example_config,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        print(f"Example configuration created at: {path}")

    def __del__(self) -> None:
        """Cleanup on deletion"""
        self.disable_watch()
