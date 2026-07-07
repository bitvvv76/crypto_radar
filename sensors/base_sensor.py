from dataclasses import dataclass, field
from typing import List


@dataclass
class SensorResult:
    """
    Единый результат работы сенсора.

    Сенсор не дает команду покупать.
    Сенсор помогает классифицировать идею:
    - ok
    - warning
    - reject
    """

    pair: str
    sensor: str
    status: str
    flags: List[str] = field(default_factory=list)
    risk_comment: str = ""
    recommendation: str = ""

    def to_text(self) -> str:
        flags_text = "\n".join(f"- {flag}" for flag in self.flags) if self.flags else "- нет"

        return (
            f"PAIR: {self.pair}\n"
            f"SENSOR: {self.sensor}\n"
            f"STATUS: {self.status}\n"
            f"FLAGS:\n{flags_text}\n"
            f"RISK COMMENT:\n{self.risk_comment}\n"
            f"RECOMMENDATION:\n{self.recommendation}"
        )


class BaseSensor:
    """
    Базовый класс для будущих сенсоров Crypto Radar.

    Каждый сенсор должен реализовать метод analyze().
    """

    name = "Base Sensor"

    def analyze(self, idea: dict) -> SensorResult:
        raise NotImplementedError("Sensor must implement analyze()")