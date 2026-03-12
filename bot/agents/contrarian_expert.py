"""Contrarian Expert — detects traps, divergences, and over-crowded setups."""

from bot.agents.base_expert import BaseExpert, Argument, ArgumentType, Vote, Verdict


class ContrarianExpert(BaseExpert):
    name = "contrarian"
    role = "Контрарианец"
    weight = 0.8  # lower weight — contrarian is a check, not primary driver

    def analyze(self, signal, market_data):
        evidence = {}
        confidence = 0.5
        reasons = []
        traps = 0
        direction = signal.get("direction", "").upper()

        # 1. Volume divergence (price up but volume falling = potential trap)
        volume_trend = market_data.get("volume_trend")  # "rising", "falling", "flat"
        regime = market_data.get("regime", "")
        adx = market_data.get("adx", 0)

        if volume_trend == "falling" and direction == "LONG" and regime == "bull_trend":
            traps += 1
            confidence -= 0.15
            reasons.append("Бычья ловушка: объём падает в бычьем тренде")
            evidence["volume_divergence"] = True
        elif volume_trend == "falling" and direction == "SHORT" and regime == "bear_trend":
            traps += 1
            confidence -= 0.15
            reasons.append("Медвежья ловушка: объём падает в медвежьем тренде")
            evidence["volume_divergence"] = True

        # 2. Exhaustion detection (ADX too high = trend exhaustion)
        if adx and float(adx) > 50:
            traps += 1
            confidence -= 0.15
            reasons.append(f"Возможное истощение тренда (ADX={float(adx):.0f})")
            evidence["trend_exhaustion"] = True

        # 3. Counter-consensus check (everyone agrees = dangerous)
        consensus_strength = market_data.get("consensus_strength", 0)
        if consensus_strength and float(consensus_strength) > 0.85:
            traps += 1
            confidence -= 0.1
            reasons.append("Слишком сильный консенсус — осторожно")
            evidence["overcrowded"] = True

        # 4. Regime transition proximity
        regime_age = market_data.get("regime_duration_seconds", 0)
        if regime_age and float(regime_age) < 600:  # less than 10 min
            traps += 1
            confidence -= 0.1
            reasons.append(f"Режим слишком молодой ({float(regime_age):.0f}s)")
            evidence["young_regime"] = True

        # 5. Check if price at round number (psychological trap)
        price = signal.get("price", 0)
        if price and float(price) > 0:
            price_f = float(price)
            # Check if within 0.1% of a round number
            magnitude = 10 ** (len(str(int(price_f))) - 1)
            nearest_round = round(price_f / magnitude) * magnitude
            dist_pct = abs(price_f - nearest_round) / price_f
            if dist_pct < 0.001:
                reasons.append(f"Цена у круглого числа ({nearest_round})")
                evidence["round_number_trap"] = True

        if not traps:
            confidence = 0.6
            reasons.append("Ловушек не обнаружено")

        evidence["traps_detected"] = traps
        confidence = max(0.0, min(1.0, confidence))
        thesis = "; ".join(reasons) if reasons else "Чисто"
        self._last_analysis = {"confidence": confidence, "traps": traps}

        return Argument(
            expert_name=self.name,
            argument_type=ArgumentType.RAISED,
            thesis=thesis,
            confidence=confidence,
            evidence=evidence,
        )

    def challenge(self, argument, signal, market_data):
        if argument.expert_name == self.name:
            return None

        traps = self._last_analysis.get("traps", 0)
        # Challenge if we found traps but expert is very confident
        if traps >= 2 and argument.confidence > 0.7 and argument.argument_type == ArgumentType.RAISED:
            return Argument(
                expert_name=self.name,
                argument_type=ArgumentType.CHALLENGED,
                thesis=f"Обнаружено {traps} ловушек — осторожнее",
                confidence=0.6,
                evidence={"traps": traps},
                challenges=[argument.argument_id],
            )
        return None

    def vote(self, signal, market_data, arguments):
        traps = self._last_analysis.get("traps", 0)
        conf = self._last_analysis.get("confidence", 0.5)

        if traps >= 3:
            verdict = Verdict.REJECT
        elif traps >= 1:
            verdict = Verdict.DEFER
        else:
            verdict = Verdict.APPROVE

        return Vote(
            expert_name=self.name,
            verdict=verdict,
            confidence=conf,
            reasoning=f"Контрарианская проверка: {traps} ловушек",
            key_factors=[f"{traps} traps"],
        )
