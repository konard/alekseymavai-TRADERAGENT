"""SMC Structuralist Expert — analyzes market structure, OB, FVG, liquidity."""

from bot.agents.base_expert import BaseExpert, Argument, ArgumentType, Vote, Verdict


class SMCExpert(BaseExpert):
    name = "smc_structuralist"
    role = "Структурист (SMC)"
    weight = 1.2  # slightly higher weight — structural analysis is foundational

    def analyze(self, signal, market_data):
        evidence = {}
        confidence = 0.5
        reasons = []

        smc_ctx = market_data.get("smc_context")
        price = signal.get("price", 0)
        direction = signal.get("direction", "").upper()

        # 1. Check proximity to order blocks
        if smc_ctx:
            obs = getattr(smc_ctx, "order_blocks", []) if smc_ctx else []
            demand_obs = [ob for ob in obs if getattr(ob, "type", "") == "demand"]
            supply_obs = [ob for ob in obs if getattr(ob, "type", "") == "supply"]

            evidence["demand_zones"] = len(demand_obs)
            evidence["supply_zones"] = len(supply_obs)

            # Check if price is near a demand zone for LONG
            if direction == "LONG" and demand_obs:
                nearest = min(demand_obs, key=lambda ob: abs(getattr(ob, "price", 0) - price))
                dist_pct = abs(getattr(nearest, "price", 0) - price) / price if price > 0 else 1
                if dist_pct < 0.02:  # within 2%
                    confidence += 0.2
                    reasons.append(f"Цена у demand zone ({dist_pct:.1%})")
                    evidence["near_demand"] = True

            # Check if price is near supply zone for SHORT
            if direction == "SHORT" and supply_obs:
                nearest = min(supply_obs, key=lambda ob: abs(getattr(ob, "price", 0) - price))
                dist_pct = abs(getattr(nearest, "price", 0) - price) / price if price > 0 else 1
                if dist_pct < 0.02:
                    confidence += 0.2
                    reasons.append(f"Цена у supply zone ({dist_pct:.1%})")
                    evidence["near_supply"] = True

            # 2. Check FVG (imbalances)
            fvgs = getattr(smc_ctx, "imbalances", []) if smc_ctx else []
            unfilled = [f for f in fvgs if not getattr(f, "filled", True)]
            evidence["unfilled_fvg"] = len(unfilled)
            if unfilled:
                confidence += 0.1
                reasons.append(f"{len(unfilled)} незакрытых FVG")

            # 3. Check liquidity levels
            liq_levels = getattr(smc_ctx, "liquidity_levels", []) if smc_ctx else []
            unswept = [lvl for lvl in liq_levels if not getattr(lvl, "swept", True)]
            evidence["unswept_liquidity"] = len(unswept)

            # 4. Market structure (BOS/CHoCH)
            structural = getattr(smc_ctx, "structural_levels", [])
            evidence["structural_levels"] = len(structural) if structural else 0

            # BOS confirms trend continuation
            smc_signal = market_data.get("smc_signal")
            if smc_signal == "BOS":
                if direction == "LONG":
                    confidence += 0.15
                    reasons.append("BOS подтверждает продолжение тренда")
                evidence["bos_detected"] = True
            elif smc_signal == "CHoCH":
                if direction == "SHORT":
                    confidence += 0.15
                    reasons.append("CHoCH подтверждает разворот")
                elif direction == "LONG":
                    confidence -= 0.15
                    reasons.append("CHoCH против LONG направления")
                evidence["choch_detected"] = True
        else:
            reasons.append("SMC контекст недоступен")
            confidence = 0.3

        confidence = max(0.0, min(1.0, confidence))
        thesis = "; ".join(reasons) if reasons else "Нейтральная структура"
        evidence["direction"] = direction

        self._last_analysis = evidence

        return Argument(
            expert_name=self.name,
            argument_type=ArgumentType.RAISED,
            thesis=thesis,
            confidence=confidence,
            evidence=evidence,
        )

    def challenge(self, argument, signal, market_data):
        """Challenge if another expert ignores structural risks."""
        if argument.expert_name == self.name:
            return None

        direction = signal.get("direction", "").upper()
        smc_ctx = market_data.get("smc_context")

        # Challenge high-confidence LONG if near supply zone
        if argument.confidence > 0.7 and direction == "LONG" and smc_ctx:
            supply_obs = [
                ob
                for ob in getattr(smc_ctx, "order_blocks", [])
                if getattr(ob, "type", "") == "supply"
            ]
            price = signal.get("price", 0)
            if supply_obs and price > 0:
                nearest = min(supply_obs, key=lambda ob: abs(getattr(ob, "price", 0) - price))
                dist_pct = abs(getattr(nearest, "price", 0) - price) / price
                if dist_pct < 0.015:
                    return Argument(
                        expert_name=self.name,
                        argument_type=ArgumentType.CHALLENGED,
                        thesis=f"LONG рискован: цена в {dist_pct:.1%} от supply zone",
                        confidence=0.6,
                        evidence={"near_supply": True, "distance_pct": dist_pct},
                        challenges=[argument.argument_id],
                    )
        return None

    def vote(self, signal, market_data, arguments):
        analysis = self._last_analysis

        has_structure = (
            analysis.get("near_demand")
            or analysis.get("near_supply")
            or analysis.get("bos_detected")
        )
        has_risk = analysis.get("near_supply") and signal.get("direction", "").upper() == "LONG"

        if has_structure and not has_risk:
            verdict = Verdict.APPROVE
            confidence = min(
                0.9,
                0.5
                + 0.1 * len([a for a in arguments if a.argument_type == ArgumentType.SUPPORTED]),
            )
        elif has_risk:
            verdict = Verdict.REJECT
            confidence = 0.7
        else:
            verdict = Verdict.DEFER
            confidence = 0.4

        factors = []
        if analysis.get("near_demand"):
            factors.append("demand zone")
        if analysis.get("bos_detected"):
            factors.append("BOS confirmed")
        if analysis.get("choch_detected"):
            factors.append("CHoCH detected")
        if analysis.get("unfilled_fvg", 0) > 0:
            factors.append(f"{analysis['unfilled_fvg']} FVG")

        return Vote(
            expert_name=self.name,
            verdict=verdict,
            confidence=confidence,
            reasoning=f"Структурный анализ: {'за' if verdict == Verdict.APPROVE else 'против' if verdict == Verdict.REJECT else 'воздержался'}",
            key_factors=factors,
        )
