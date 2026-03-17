"""Trend Expert — analyzes EMA, ADX, momentum for trend confirmation."""

from typing import Any

from bot.agents.base_expert import Argument, ArgumentType, BaseExpert, Verdict, Vote


class TrendExpert(BaseExpert):
    name = "trend_analyst"
    role = "Трендовик"
    weight = 1.0

    def analyze(self, signal: dict[str, Any], market_data: dict[str, Any]) -> Argument:
        evidence = {}
        confidence = 0.5
        reasons = []
        direction = signal.get("direction", "").upper()

        adx = market_data.get("adx")
        regime = market_data.get("regime", "")
        trend_strength = market_data.get("trend_strength", 0)
        ema_spread = market_data.get("ema_spread", 0)  # EMA20-EMA50 divergence

        # ADX analysis
        if adx is not None:
            evidence["adx"] = round(float(adx), 2)
            if adx > 25:
                confidence += 0.15
                reasons.append(f"Тренд подтверждён (ADX={adx:.1f})")
                if adx > 40:
                    confidence += 0.1
                    reasons.append("Сильный тренд")
            else:
                confidence -= 0.1
                reasons.append(f"Слабый тренд (ADX={adx:.1f})")

        # Regime alignment
        evidence["regime"] = regime
        if direction == "LONG" and regime in ("bull_trend", "accumulation"):
            confidence += 0.15
            reasons.append(f"Режим {regime} поддерживает LONG")
        elif direction == "SHORT" and regime in ("bear_trend", "distribution"):
            confidence += 0.15
            reasons.append(f"Режим {regime} поддерживает SHORT")
        elif direction == "LONG" and regime in ("bear_trend", "distribution"):
            confidence -= 0.2
            reasons.append(f"Режим {regime} ПРОТИВ LONG")
        elif direction == "SHORT" and regime in ("bull_trend", "accumulation"):
            confidence -= 0.2
            reasons.append(f"Режим {regime} ПРОТИВ SHORT")

        # EMA spread
        if ema_spread:
            evidence["ema_spread"] = round(float(ema_spread), 4)
            if direction == "LONG" and ema_spread > 0:
                confidence += 0.1
                reasons.append("EMA20 > EMA50 (бычий)")
            elif direction == "SHORT" and ema_spread < 0:
                confidence += 0.1
                reasons.append("EMA20 < EMA50 (медвежий)")
            elif direction == "LONG" and ema_spread < 0:
                confidence -= 0.1
                reasons.append("EMA20 < EMA50 (против LONG)")

        # Trend strength
        if trend_strength:
            evidence["trend_strength"] = round(float(trend_strength), 2)

        confidence = max(0.0, min(1.0, confidence))
        thesis = "; ".join(reasons) if reasons else "Нет выраженного тренда"
        self._last_analysis = {
            "confidence": confidence,
            "direction": direction,
            "adx": adx,
            "regime": regime,
        }

        return Argument(
            expert_name=self.name,
            argument_type=ArgumentType.RAISED,
            thesis=thesis,
            confidence=confidence,
            evidence=evidence,
        )

    def challenge(
        self, argument: Argument, signal: dict[str, Any], market_data: dict[str, Any]
    ) -> Argument | None:
        if argument.expert_name == self.name:
            return None

        direction = signal.get("direction", "").upper()
        adx = market_data.get("adx", 0)
        regime = market_data.get("regime", "")

        # Challenge if someone approves entry in counter-trend
        if argument.confidence > 0.6:
            if (
                direction == "LONG"
                and regime in ("bear_trend", "distribution")
                and adx
                and adx > 25
            ):
                return Argument(
                    expert_name=self.name,
                    argument_type=ArgumentType.CHALLENGED,
                    thesis=f"Вход против тренда: режим {regime}, ADX={adx:.1f}",
                    confidence=0.7,
                    evidence={"counter_trend": True, "adx": adx, "regime": regime},
                    challenges=[argument.argument_id],
                )
        return None

    def vote(
        self, signal: dict[str, Any], market_data: dict[str, Any], arguments: list[Argument]
    ) -> Vote:
        analysis = self._last_analysis
        conf = analysis.get("confidence", 0.5)

        if conf >= 0.65:
            verdict = Verdict.APPROVE
        elif conf <= 0.35:
            verdict = Verdict.REJECT
        else:
            verdict = Verdict.DEFER

        factors = []
        if analysis.get("adx"):
            factors.append(f"ADX={analysis['adx']:.0f}")
        if analysis.get("regime"):
            factors.append(f"regime={analysis['regime']}")

        return Vote(
            expert_name=self.name,
            verdict=verdict,
            confidence=conf,
            reasoning=f"Трендовый анализ: {verdict.value}",
            key_factors=factors,
        )
