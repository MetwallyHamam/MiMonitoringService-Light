from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Any


def duration_seconds(value: Any) -> int:
    try:
        raw = f"{int(value or 0):06d}"
        return int(raw[:-4]) * 3600 + int(raw[-4:-2]) * 60 + int(raw[-2:])
    except (TypeError, ValueError):
        return 0


def job_intelligence(rows: list[dict[str, Any]], overdue_hours: float = 24, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    ordered = sorted(rows, key=lambda row: str(row.get("run_at", "")), reverse=True)
    completed = [row for row in ordered if str(row.get("status")) != "Running"]
    durations = sorted(float(row.get("duration_seconds", 0)) for row in completed)
    successes = sum(1 for row in completed if str(row.get("status")) == "Succeeded")
    p95 = durations[min(len(durations) - 1, max(0, int(len(durations) * 0.95)))] if durations else None
    typical = median(durations) if durations else None
    latest = ordered[0] if ordered else None
    latest_at = None
    if latest and latest.get("run_at"):
        try:
            latest_at = datetime.fromisoformat(str(latest["run_at"]))
            if latest_at.tzinfo is None: latest_at = latest_at.replace(tzinfo=UTC)
        except ValueError:
            pass
    age_hours = (now - latest_at.astimezone(UTC)).total_seconds() / 3600 if latest_at else None
    streak = 0
    for row in completed:
        if str(row.get("status")) == "Succeeded": break
        streak += 1
    abnormal = bool(latest and typical and float(latest.get("duration_seconds", 0)) >= max(typical * 2, p95 or 0))
    recent_failures = 0
    for row in completed:
        try:
            run_at = datetime.fromisoformat(str(row.get("run_at", "")))
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=UTC)
        except ValueError:
            continue
        if (now - run_at.astimezone(UTC)).total_seconds() <= 86400 and str(row.get("status")) != "Succeeded":
            recent_failures += 1
    recovered = bool(completed and str(completed[0].get("status")) == "Succeeded" and any(str(row.get("status")) != "Succeeded" for row in completed[1:]))
    return {
        "executions": len(completed), "success_percent": round(100 * successes / len(completed), 2) if completed else None,
        "median_seconds": round(typical, 2) if typical is not None else None,
        "p95_seconds": round(p95, 2) if p95 is not None else None,
        "failure_streak": streak, "latest_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "overdue": age_hours is None or age_hours > overdue_hours,
        "abnormal_duration": abnormal,
        "failures_24h": recent_failures,
        "recovered": recovered,
    }


def schedule_assessment(job: dict[str, Any], *, now: datetime | None = None, grace_minutes: int = 15) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if job.get("job_enabled") is False:
        return {"status": "Job disabled", "missed": False, "overlap": False}
    if job.get("schedule_count") == 0:
        return {"status": "No schedule", "missed": False, "overlap": False}
    if job.get("enabled_schedule_count") == 0:
        return {"status": "Schedule disabled", "missed": False, "overlap": False}
    try:
        scheduled = datetime.fromisoformat(str(job.get("next_run", "")))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=now.tzinfo)
        scheduled = scheduled.astimezone(UTC)
    except ValueError:
        return {"status": "Schedule unavailable", "missed": False, "overlap": False}
    overdue_seconds = (now - scheduled).total_seconds()
    past_grace = overdue_seconds > max(0, grace_minutes) * 60
    running = job.get("status") == "Running"
    return {
        "status": "Overlapping next occurrence" if running and past_grace else "Missed occurrence" if past_grace else "On schedule",
        "missed": bool(past_grace and not running), "overlap": bool(past_grace and running),
        "scheduled_for": scheduled.isoformat(timespec="seconds"),
        "minutes_late": round(max(0, overdue_seconds) / 60, 1),
    }
