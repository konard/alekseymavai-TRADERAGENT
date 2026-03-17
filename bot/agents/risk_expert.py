"""Risk Expert — analyzes drawdown, exposure, daily loss limits."""

from bot.agents.base_expert import Argument, ArgumentType, BaseExpert, Verdict, Vote


class RiskExpert(BaseExpert):
    name = "risk_manager"
    role = "Риск-менеджер"
    weight = 1.5  # highest weight — risk has veto power

    def analyze(self, signal, market_data):
        evidence = {}
        confidence = 0.5
        reasons = []
        red_flags = 0

        # Current portfolio state
        balance = market_data.get("balance", 0)
        initial_balance = market_data.get("initial_balance", 0)
        daily_loss = market_data.get("daily_loss", 0)
        max_daily_loss = market_data.get("max_daily_loss", 0)
        drawdown = market_data.get("drawdown", 0)
        open_positions = market_data.get("open_positions", 0)
        max_positions = market_data.get("max_positions", 10)
        exposure_pct = market_data.get("exposure_pct", 0)

        # Signal risk assessment
        risk_per_trade = signal.get("risk_pct", 2.0)
        stop_loss = signal.get("stop_loss")
        take_profit = signal.get("take_profit")

        evidence["balance"] = balance
        evidence["drawdown"] = round(float(drawdown), 4) if drawdown else 0
        evidence["daily_loss"] = round(float(daily_loss), 2) if daily_loss else 0
        evidence["open_positions"] = open_positions
        evidence["exposure_pct"] = round(float(exposure_pct), 2) if exposure_pct else 0

        # 1. Drawdown check
        if drawdown and float(drawdown) > 0.15:
            red_flags += 1
            confidence -= 0.2
            reasons.append(f"Высокая просадка: {float(drawdown):.1%}")
        elif drawdown and float(drawdown) > 0.08:
            confidence -= 0.1
            reasons.append(f"Просадка повышена: {float(drawdown):.1%}")
        else:
            confidence += 0.1
            reasons.append("Просадка в норме")

        # 2. Daily loss check
        if max_daily_loss and daily_loss:
            daily_usage = (
                float(daily_loss) / float(max_daily_loss) if float(max_daily_loss) > 0 else 0
            )
            evidence["daily_loss_usage"] = round(daily_usage, 2)
            if daily_usage > 0.7:
                red_flags += 1
                confidence -= 0.25
                reasons.append(f"Дневной лимит на {daily_usage:.0%}")
            elif daily_usage > 0.4:
                confidence -= 0.1
                reasons.append(f"Дневной лимит использован на {daily_usage:.0%}")

        # 3. Position count check
        if open_positions >= max_positions:
            red_flags += 1
            confidence -= 0.3
            reasons.append(f"Лимит позиций: {open_positions}/{max_positions}")

        # 4. Exposure check
        if exposure_pct and float(exposure_pct) > 80:
            red_flags += 1
            confidence -= 0.2
            reasons.append(f"Высокая экспозиция: {float(exposure_pct):.0f}%")

        # 5. Risk:Reward check
        if stop_loss and take_profit and signal.get("price"):
            price = float(signal["price"])
            sl = float(stop_loss)
            tp = float(take_profit)
            risk = abs(price - sl)
            reward = abs(tp - price)
            rr = reward / risk if risk > 0 else 0
            evidence["risk_reward"] = round(rr, 2)
            if rr < 1.5:
                confidence -= 0.15
                reasons.append(f"R:R слабый ({rr:.1f})")
            elif rr >= 2.0:
                confidence += 0.15
                reasons.append(f"R:R хороший ({rr:.1f})")

        # 6. Balance sufficient
        if balance and balance > 0:
            trade_size = float(risk_per_trade) / 100 * float(balance)
            if trade_size < 5:
                red_flags += 1
                reasons.append("Баланс недостаточен")

        evidence["red_flags"] = red_flags
        confidence = max(0.0, min(1.0, confidence))
        thesis = "; ".join(reasons) if reasons else "Риски в норме"
        self._last_analysis = {"confidence": confidence, "red_flags": red_flags}

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

        # Risk expert challenges any high-confidence argument when red flags exist
        red_flags = self._last_analysis.get("red_flags", 0)
        if argument.confidence > 0.7 and red_flags >= 2:
            return Argument(
                expert_name=self.name,
                argument_type=ArgumentType.CHALLENGED,
                thesis=f"Высокий risk: {red_flags} red flags при confidence={argument.confidence:.2f}",
                confidence=0.8,
                evidence={"red_flags": red_flags, "challenged_confidence": argument.confidence},
                challenges=[argument.argument_id],
            )
        return None

    def vote(self, signal, market_data, arguments):
        analysis = self._last_analysis
        red_flags = analysis.get("red_flags", 0)
        conf = analysis.get("confidence", 0.5)

        # Risk manager has effective veto on 3+ red flags
        if red_flags >= 3:
            verdict = Verdict.REJECT
            confidence = 0.95
        elif red_flags >= 2:
            verdict = Verdict.REJECT
            confidence = 0.7
        elif conf >= 0.6:
            verdict = Verdict.APPROVE
            confidence = conf
        else:
            verdict = Verdict.DEFER
            confidence = 0.5

        return Vote(
            expert_name=self.name,
            verdict=verdict,
            confidence=confidence,
            reasoning=f"Риск-оценка: {red_flags} red flags",
            key_factors=[f"{red_flags} red flags", f"confidence={conf:.2f}"],
        )
