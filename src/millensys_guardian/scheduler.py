from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .actions import pool_action, restart_service, service_action
from .store import Store


@dataclass(frozen=True)
class DueAction:
    action: str
    scheduled_for: datetime
    grace_minutes: int


def configured_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _clock(value: str, default: time = time(2, 0)) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def _monthly_at(day: int, reference: date, clock: time, tz: ZoneInfo) -> datetime:
    actual_day = min(max(day, 1), calendar.monthrange(reference.year, reference.month)[1])
    return datetime.combine(reference.replace(day=actual_day), clock, tz)


def latest_occurrence(schedule: dict, now: datetime) -> datetime | None:
    """Return the latest recurrence not later than `now`. All values are timezone-aware."""
    if not schedule.get("enabled"):
        return None
    clock = _clock(schedule.get("time", "02:00"))
    frequency = str(schedule.get("frequency", "Daily")).lower()
    if frequency == "interval":
        minutes = max(5, int(schedule.get("interval_minutes", 60)))
        anchor_text = schedule.get("anchor")
        try:
            anchor = (
                datetime.fromisoformat(anchor_text)
                if anchor_text
                else datetime(2020, 1, 1, tzinfo=UTC)
            )
        except ValueError:
            anchor = datetime(2020, 1, 1, tzinfo=UTC)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=now.tzinfo)
        elapsed = (now - anchor.astimezone(now.tzinfo)).total_seconds()
        if elapsed < 0:
            return None
        return anchor.astimezone(now.tzinfo) + timedelta(
            minutes=minutes * int(elapsed // (minutes * 60))
        )
    if frequency == "daily":
        candidate = datetime.combine(now.date(), clock, now.tzinfo)
        return candidate if candidate <= now else candidate - timedelta(days=1)
    if frequency == "weekly":
        weekday = min(max(int(schedule.get("weekday", 0)), 0), 6)
        days_back = (now.weekday() - weekday) % 7
        candidate = datetime.combine(now.date() - timedelta(days=days_back), clock, now.tzinfo)
        return candidate if candidate <= now else candidate - timedelta(days=7)
    if frequency == "monthly":
        day = int(schedule.get("month_day", 1))
        candidate = _monthly_at(day, now.date(), clock, now.tzinfo)
        if candidate <= now:
            return candidate
        previous_month = (now.replace(day=1) - timedelta(days=1)).date()
        return _monthly_at(day, previous_month, clock, now.tzinfo)
    return None


def due_action(schedule: dict, now: datetime) -> DueAction | None:
    occurrence = latest_occurrence(schedule, now)
    if occurrence is None:
        return None
    grace = min(max(int(schedule.get("missed_run_minutes", 10)), 1), 1440)
    if now - occurrence > timedelta(minutes=grace):
        return None
    return DueAction(str(schedule.get("action", "restart")).lower(), occurrence, grace)


def maintenance_bounds(maintenance: dict, now: datetime) -> tuple[datetime, datetime] | None:
    if not maintenance.get("enabled"):
        return None
    start_clock = _clock(maintenance.get("start", "00:00"), time(0, 0))
    end_clock = _clock(maintenance.get("end", "00:30"), time(0, 30))
    allowed_days = maintenance.get("weekdays", list(range(7)))
    for days_back in range(8):
        start_date = now.date() - timedelta(days=days_back)
        if start_date.weekday() not in allowed_days:
            continue
        start = datetime.combine(start_date, start_clock, now.tzinfo)
        end_date = start_date + timedelta(days=1) if end_clock <= start_clock else start_date
        end = datetime.combine(end_date, end_clock, now.tzinfo)
        if start <= now <= end:
            return start, end
    return None


def in_maintenance(maintenance: dict, now: datetime) -> bool:
    return maintenance_bounds(maintenance, now) is not None


def latest_finished_maintenance(
    maintenance: dict, now: datetime
) -> tuple[datetime, datetime] | None:
    if not maintenance.get("enabled"):
        return None
    start_clock = _clock(maintenance.get("start", "00:00"), time(0, 0))
    end_clock = _clock(maintenance.get("end", "00:30"), time(0, 30))
    allowed_days = maintenance.get("weekdays", list(range(7)))
    candidates: list[tuple[datetime, datetime]] = []
    for days_back in range(8):
        start_date = now.date() - timedelta(days=days_back)
        if start_date.weekday() not in allowed_days:
            continue
        start = datetime.combine(start_date, start_clock, now.tzinfo)
        end_date = start_date + timedelta(days=1) if end_clock <= start_clock else start_date
        end = datetime.combine(end_date, end_clock, now.tzinfo)
        if end <= now:
            candidates.append((start, end))
    return max(candidates, key=lambda item: item[1]) if candidates else None


class SafeScheduler:
    def __init__(self, store: Store, timezone_name: str = "UTC") -> None:
        self.store = store
        self.timezone = configured_timezone(timezone_name)

    def process(
        self,
        kind: str,
        target: str,
        config: dict,
        now: datetime | None = None,
        *,
        allow_actions: bool = True,
    ) -> bool:
        """Run claimed boundary/scheduled actions and return whether monitoring is suppressed."""
        current = (now or datetime.now(UTC)).astimezone(self.timezone)
        maintenance = config.get("maintenance", {})
        bounds = maintenance_bounds(maintenance, current)
        if bounds:
            start, _ = bounds
            if allow_actions and maintenance.get("stop_start"):
                self._execute_once(kind, target, "stop", start, "maintenance", config)
            return bool(maintenance.get("suppress_monitoring", True))

        # Start after a stop/start maintenance window if its end is within the missed-run grace.
        if allow_actions and maintenance.get("enabled") and maintenance.get("stop_start"):
            grace = timedelta(
                minutes=min(max(int(maintenance.get("resume_grace_minutes", 30)), 1), 1440)
            )
            prior_bounds = latest_finished_maintenance(maintenance, current)
            if prior_bounds:
                _, end = prior_bounds
                if current - end <= grace:
                    self._execute_once(kind, target, "start", end, "maintenance", config)

        schedule = config.get("schedule", {})
        due = due_action(schedule, current)
        if allow_actions and due:
            self._execute_once(kind, target, due.action, due.scheduled_for, "schedule", config)
        return False

    def _execute_once(
        self,
        kind: str,
        target: str,
        action: str,
        scheduled_for: datetime,
        source: str,
        config: dict,
    ) -> None:
        stamp = scheduled_for.astimezone(UTC).isoformat(timespec="minutes")
        raw_key = f"{source}|{kind}|{target}|{action}|{stamp}"
        claim_key = hashlib.sha256(raw_key.encode()).hexdigest()
        if not self.store.claim_action(
            claim_key,
            kind,
            target,
            action,
            stamp,
            retry_failed=True,
            max_attempts=int(config.get("action_retry_count", 3)),
            retry_delay_seconds=int(config.get("action_retry_delay_seconds", 60)),
        ):
            return
        if kind == "service":
            ok, result = (
                restart_service(target, config.get("related_services", []))
                if action == "restart" and config.get("related_services")
                else restart_service(target)
                if action == "restart"
                else service_action(target, action)
            )
        elif kind == "pool":
            pool_command = "recycle" if action in {"restart", "recycle"} else action
            ok, result = pool_action(target, pool_command)
        else:
            ok, result = False, f"Unsupported scheduled target kind: {kind}"
        self.store.finish_action(claim_key, ok, result)
        self.store.add_event(
            "info" if ok else "error",
            "scheduled_action",
            target,
            f"{source.title()} {action} requested",
            scheduled_for=stamp,
            result=result,
        )
