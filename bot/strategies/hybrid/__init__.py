"""Hybrid Strategy Package — v2.0 Market Regime Detection & Strategy Selection."""

from bot.strategies.hybrid.hybrid_config import HybridConfig, HybridMode
from bot.strategies.hybrid.hybrid_strategy import (
    HybridAction,
    HybridStrategy,
    TransitionEvent,
)
from bot.strategies.hybrid.market_regime_detector import (
    BBWidth,
    MarketIndicators,
    MarketRegimeDetectorV2,
    RegimeChangeEvent,
    RegimeConfig,
    RegimeResult,
    RegimeType,
    StrategyRecommendation,
    VolumeProfile,
)
from bot.strategies.hybrid.recovery_config import RecoveryConfig
from bot.strategies.hybrid.recovery_coordinator import (
    RecoveryAction,
    RecoveryCoordinator,
    RecoveryPhase,
    RecoveryState,
    UnderwaterPosition,
)

__all__ = [
    # Market Regime Detector
    "MarketRegimeDetectorV2",
    "RegimeType",
    "StrategyRecommendation",
    "RegimeResult",
    "RegimeConfig",
    "MarketIndicators",
    "BBWidth",
    "VolumeProfile",
    "RegimeChangeEvent",
    # Hybrid Strategy
    "HybridStrategy",
    "HybridConfig",
    "HybridMode",
    "HybridAction",
    "TransitionEvent",
    # Recovery
    "RecoveryConfig",
    "RecoveryCoordinator",
    "RecoveryPhase",
    "RecoveryState",
    "RecoveryAction",
    "UnderwaterPosition",
]
