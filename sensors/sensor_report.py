from sensors.scam_risk_sensor import ScamRiskSensor


def run_case(title: str, idea: dict):
    print("=" * 60)
    print(title)
    print("=" * 60)

    sensor = ScamRiskSensor()
    result = sensor.analyze(idea)

    print(result.to_text())
    print()


def main():
    """
    Ручной тест Sensors Core v0.1.

    Этот файл не подключен к боевому автосканеру.
    Используется только для проверки логики сенсора.
    """

    ok_idea = {
        "pair_symbol": "AI/USDC",
        "final_score": 80,
        "risk_score": 20,
        "liquidity_usd": 150_000,
        "volume_24h": 50_000,
        "price_change_24h": 12.5,
    }

    warning_idea = {
        "pair_symbol": "HOT/USDC",
        "final_score": 80,
        "risk_score": 20,
        "liquidity_usd": 120_000,
        "volume_24h": 8_000,
        "price_change_24h": 45.0,
    }

    reject_idea = {
        "pair_symbol": "USDC/SCAM",
        "final_score": 55,
        "risk_score": 85,
        "liquidity_usd": 5_000,
        "volume_24h": 0,
        "price_change_24h": 120.0,
    }

    run_case("CASE 1: нормальная идея", ok_idea)
    run_case("CASE 2: перегретая идея / warning", warning_idea)
    run_case("CASE 3: слабая или мусорная идея / reject", reject_idea)


if __name__ == "__main__":
    main()