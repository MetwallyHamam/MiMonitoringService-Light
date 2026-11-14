from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any

from .scheduler import configured_timezone


def server_timezone(config: dict[str, Any]) -> tzinfo:
    """Return the timezone selected for the monitored server."""
    return configured_timezone(config.get("agent", {}).get("timezone", "SERVER_LOCAL"))


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def server_time_text(
    value: str | datetime | None,
    config: dict[str, Any],
    pattern: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    if not value:
        return "—"
    try:
        return parse_timestamp(value).astimezone(server_timezone(config)).strftime(pattern)
    except (TypeError, ValueError):
        return str(value)
