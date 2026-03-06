"""
Tests for SMCConfigSchema Pydantic validation and conversion to SMCConfig dataclass.
"""

from decimal import Decimal

import pytest

from bot.config.schemas import BotConfig, SMCConfigSchema, StrategyType
from bot.strategies.smc.config import SMCConfig


class TestSMCConfigSchema:
    """Tests for Pydantic SMCConfigSchema validation."""

    def test_defaults(self):
        schema = SMCConfigSchema()
        assert schema.enabled is True
        assert schema.swing_length == 10
        assert schema.trend_period == 20
        assert schema.close_break is True
        assert schema.risk_per_trade_pct == Decimal("0.02")
        assert schema.min_risk_reward == Decimal("2.0")
        assert schema.max_position_size == Decimal("10000")
        assert schema.max_positions == 3
        assert schema.use_trailing_stop is True

    def test_custom_values(self):
        schema = SMCConfigSchema(
            swing_length=30,
            risk_per_trade_pct=Decimal("0.03"),
            max_positions=5,
        )
        assert schema.swing_length == 30
        assert schema.risk_per_trade_pct == Decimal("0.03")
        assert schema.max_positions == 5

    def test_swing_length_validation_min(self):
        with pytest.raises(ValueError):
            SMCConfigSchema(swing_length=2)

    def test_swing_length_validation_max(self):
        with pytest.raises(ValueError):
            SMCConfigSchema(swing_length=300)

    def test_max_positions_validation_min(self):
        with pytest.raises(ValueError):
            SMCConfigSchema(max_positions=0)

    def test_max_positions_validation_max(self):
        with pytest.raises(ValueError):
            SMCConfigSchema(max_positions=20)

    def test_timeframe_defaults(self):
        schema = SMCConfigSchema()
        assert schema.trend_timeframe == "1d"
        assert schema.structure_timeframe == "4h"
        assert schema.working_timeframe == "1h"
        assert schema.entry_timeframe == "15m"


class TestSMCConfigSchemaToDataclass:
    """Tests for converting Pydantic SMCConfigSchema to SMCConfig dataclass."""

    def test_conversion(self):
        schema = SMCConfigSchema(
            swing_length=40,
            risk_per_trade_pct=Decimal("0.03"),
            max_positions=5,
            close_mitigation=True,
        )
        dataclass_config = SMCConfig(
            swing_length=schema.swing_length,
            trend_period=schema.trend_period,
            close_break=schema.close_break,
            close_mitigation=schema.close_mitigation,
            join_consecutive_fvg=schema.join_consecutive_fvg,
            liquidity_range_percent=schema.liquidity_range_percent,
            risk_per_trade_pct=schema.risk_per_trade_pct,
            min_risk_reward=schema.min_risk_reward,
            max_position_size=schema.max_position_size,
            require_volume_confirmation=schema.require_volume_confirmation,
            min_volume_multiplier=schema.min_volume_multiplier,
            max_positions=schema.max_positions,
            use_trailing_stop=schema.use_trailing_stop,
            trailing_stop_activation=schema.trailing_stop_activation,
            trailing_stop_distance=schema.trailing_stop_distance,
        )
        assert dataclass_config.swing_length == 40
        assert dataclass_config.risk_per_trade_pct == Decimal("0.03")
        assert dataclass_config.max_positions == 5
        assert dataclass_config.close_mitigation is True

    def test_defaults_match(self):
        schema = SMCConfigSchema()
        dc = SMCConfig()
        assert schema.swing_length == dc.swing_length
        assert schema.trend_period == dc.trend_period
        assert schema.close_break == dc.close_break
        assert schema.risk_per_trade_pct == dc.risk_per_trade_pct
        assert schema.min_risk_reward == dc.min_risk_reward
        assert schema.max_position_size == dc.max_position_size
        assert schema.max_positions == dc.max_positions


class TestStrategyTypeEnum:
    """Tests for SMC in StrategyType enum."""

    def test_smc_in_enum(self):
        assert StrategyType.SMC == "smc"
        assert StrategyType.SMC.value == "smc"

    def test_smc_from_string(self):
        assert StrategyType("smc") == StrategyType.SMC


class TestBotConfigSMCValidation:
    """Tests for BotConfig validation with SMC strategy."""

    def _base_config(self, **overrides):
        config = {
            "name": "test_smc_bot",
            "symbol": "BTC/USDT",
            "strategy": "smc",
            "exchange": {
                "exchange_id": "bybit",
                "credentials_name": "test",
            },
            "smc": {"enabled": True},
            "risk_management": {
                "max_position_size": 5000,
                "min_order_size": 10,
            },
        }
        config.update(overrides)
        return config

    def test_smc_config_valid(self):
        config = BotConfig(**self._base_config())
        assert config.strategy == "smc"
        assert config.smc is not None

    def test_smc_config_missing_raises(self):
        data = self._base_config()
        del data["smc"]
        with pytest.raises(ValueError, match="requires smc configuration"):
            BotConfig(**data)

    def test_smc_config_none_raises(self):
        data = self._base_config(smc=None)
        with pytest.raises(ValueError, match="requires smc configuration"):
            BotConfig(**data)

    def test_smc_with_custom_params(self):
        data = self._base_config(
            smc={
                "enabled": True,
                "swing_length": 30,
                "risk_per_trade_pct": "0.03",
                "max_positions": 5,
            }
        )
        config = BotConfig(**data)
        assert config.smc.swing_length == 30
        assert config.smc.risk_per_trade_pct == Decimal("0.03")
        assert config.smc.max_positions == 5
