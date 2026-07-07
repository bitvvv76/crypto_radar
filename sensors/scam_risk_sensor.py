from sensors.base_sensor import BaseSensor, SensorResult


class ScamRiskSensor(BaseSensor):
    """
    Scam/Risk Sensor v0.1

    Проверяет базовые риск-факторы идеи.
    Не дает инвестиционных рекомендаций.
    Не подключается к боевому автосканеру.
    """

    name = "Scam/Risk Sensor"

    MIN_FINAL_SCORE = 70
    MIN_LIQUIDITY_USD = 50_000
    MIN_VOLUME_24H = 10_000
    OVERHEAT_24H_PCT = 30
    HIGH_RISK_SCORE = 70

    ALLOWED_QUOTE_TOKENS = ("USDC", "USDT", "DAI")

    def analyze(self, idea: dict) -> SensorResult:
        pair = str(idea.get("pair_symbol") or idea.get("pair") or "UNKNOWN")

        final_score = self._to_float(idea.get("final_score"))
        risk_score = self._to_float(idea.get("risk_score"))
        liquidity_usd = self._to_float(idea.get("liquidity_usd"))
        volume_24h = self._to_float(idea.get("volume_24h"))
        price_change_24h = self._to_float(idea.get("price_change_24h"))

        flags = []
        reject_reasons = []

        if final_score is None:
            flags.append("final_score отсутствует — требуется ручная проверка")
        elif final_score < self.MIN_FINAL_SCORE:
            reject_reasons.append(f"final_score ниже {self.MIN_FINAL_SCORE}")

        if price_change_24h is None:
            flags.append("price_change_24h отсутствует — нельзя оценить перегрев")
        elif price_change_24h > self.OVERHEAT_24H_PCT:
            flags.append(f"перегрев 24h: рост {price_change_24h:.2f}%")

        if liquidity_usd is None:
            flags.append("liquidity_usd отсутствует — требуется ручная проверка ликвидности")
        elif liquidity_usd < self.MIN_LIQUIDITY_USD:
            reject_reasons.append(f"низкая ликвидность: {liquidity_usd:.2f} USD")

        if volume_24h is None:
            flags.append("volume_24h отсутствует — требуется ручная проверка объема")
        elif volume_24h <= 0:
            reject_reasons.append("нулевой объем 24h")
        elif volume_24h < self.MIN_VOLUME_24H:
            flags.append(f"слабый объем 24h: {volume_24h:.2f} USD")

        if risk_score is None:
            flags.append("risk_score отсутствует — требуется ручная проверка риска")
        elif risk_score >= self.HIGH_RISK_SCORE:
            flags.append(f"высокий risk_score: {risk_score:.2f}")

        if not self._has_allowed_quote_token(pair):
            reject_reasons.append("пара не входит в разрешенные базовые активы USDC/USDT/DAI")

        if self._looks_like_reverse_or_bad_pair(pair):
            reject_reasons.append("пара похожа на мусорную или обратную")

        if reject_reasons:
            status = "reject"
            flags = reject_reasons + flags
            risk_comment = (
                "Идея не проходит базовый риск-фильтр Sensors Core v0.1. "
                "Ее нельзя переводить в paper candidate без отдельного ручного допуска."
            )
            recommendation = "Оставить вне paper candidate. Зафиксировать причину отказа."
        elif flags:
            status = "warning"
            risk_comment = (
                "Идея может быть интересной, но содержит риск-факторы. "
                "Нужна ручная проверка перед переводом в paper candidate."
            )
            recommendation = "Оставить в watchlist и провести ручную проверку."
        else:
            status = "ok"
            flags = []
            risk_comment = (
                "Критических риск-факторов на уровне Scam/Risk Sensor v0.1 не найдено."
            )
            recommendation = "Можно рассматривать для дальнейшего анализа, но не как сигнал покупки."

        return SensorResult(
            pair=pair,
            sensor=self.name,
            status=status,
            flags=flags,
            risk_comment=risk_comment,
            recommendation=recommendation,
        )

    @staticmethod
    def _to_float(value):
        if value is None:
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _has_allowed_quote_token(self, pair: str) -> bool:
        pair_upper = pair.upper()
        return any(pair_upper.endswith(f"/{quote}") for quote in self.ALLOWED_QUOTE_TOKENS)

    @staticmethod
    def _looks_like_reverse_or_bad_pair(pair: str) -> bool:
        pair_upper = pair.upper()

        bad_fragments = (
            "WETH/",
            "WBTC/",
            "USDC/",
            "USDT/",
            "DAI/",
        )

        return any(pair_upper.startswith(fragment) for fragment in bad_fragments)