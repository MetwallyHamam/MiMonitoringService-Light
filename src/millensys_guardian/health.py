from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdTransition:
    sustained: bool = False
    recovered: bool = False


class SustainedThresholds:
    """Track threshold duration without treating one noisy sample as an incident."""

    def __init__(self) -> None:
        self._breach_started: dict[str, float] = {}
        self._sustained: set[str] = set()

    def update(
        self, key: str, breached: bool, now: float, sustain_seconds: float
    ) -> ThresholdTransition:
        if not breached:
            recovered = key in self._sustained
            self._breach_started.pop(key, None)
            self._sustained.discard(key)
            return ThresholdTransition(recovered=recovered)
        started = self._breach_started.setdefault(key, now)
        if now - started >= max(0, sustain_seconds):
            self._sustained.add(key)
            return ThresholdTransition(sustained=True)
        return ThresholdTransition()
