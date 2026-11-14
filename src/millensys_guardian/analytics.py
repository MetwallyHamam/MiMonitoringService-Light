from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass(frozen=True)
class HealthAssessment:
    score: int
    label: str
    explanation: str
    contributors: tuple[str, ...]


HEALTHY_MINIMUM = 98
ATTENTION_MINIMUM = 95


def assess_health(store: Store) -> HealthAssessment:
    incidents = store.open_score_incidents()
    deductions = [f"-{row['deduction']} {row['target']} — {row['title']}" for row in incidents]
    total = sum(max(0, int(row["deduction"])) for row in incidents)

    score = max(0, 100 - min(total, 100))
    if score >= HEALTHY_MINIMUM:
        label = "Healthy"
    elif score >= ATTENTION_MINIMUM:
        label = "Attention"
    else:
        label = "Critical"
    explanation = (
        "No active incidents are reducing the score."
        if not deductions
        else (
            f"{len(deductions)} unresolved incident(s) reduce the score from 100."
        )
    )
    return HealthAssessment(score, label, explanation, tuple(deductions))
