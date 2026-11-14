from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any


def _linear_forecast(points: list[tuple[float, float]]) -> tuple[float, float]:
    count = len(points)
    mean_x = sum(point[0] for point in points) / count
    mean_y = sum(point[1] for point in points) / count
    denominator = sum((point[0] - mean_x) ** 2 for point in points)
    if denominator <= 0:
        return 0.0, 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    fitted = [mean_y + slope * (x - mean_x) for x, _y in points]
    total = sum((y - mean_y) ** 2 for _x, y in points)
    residual = sum((points[index][1] - fitted[index]) ** 2 for index in range(count))
    r_squared = 1.0 - residual / total if total > 0 else 1.0
    return slope, max(0.0, min(1.0, r_squared))


def capacity_forecasts(rows: Iterable[Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["profile_id"]), str(row["database_name"]), str(row["logical_name"]))].append(row)
    result = {}
    for key, values in grouped.items():
        values.sort(key=lambda row: str(row["day"]))
        first_day = datetime.fromisoformat(str(values[0]["day"]))
        points = [
            (
                float((datetime.fromisoformat(str(row["day"])) - first_day).days),
                float(row["end_allocated_mb"]),
            )
            for row in values
        ]
        span_days = int(points[-1][0]) if points else 0
        if len(points) < 3 or span_days < 2:
            result[key] = {
                "status": "Insufficient data",
                "confidence": "Insufficient",
                "growth_mb_per_day": 0.0,
                "days_to_limit": None,
                "limit": "",
                "r_squared": 0.0,
            }
            continue
        slope, r_squared = _linear_forecast(points)
        latest = values[-1]
        limits: list[tuple[float, str]] = []
        maximum = latest["max_size_mb"]
        if slope > 0 and maximum is not None and float(maximum) > float(latest["end_allocated_mb"]):
            limits.append(((float(maximum) - float(latest["end_allocated_mb"])) / slope, "file maximum"))
        volume_free = latest["min_volume_free_mb"]
        if slope > 0 and volume_free is not None and float(volume_free) > 0:
            limits.append((float(volume_free) / slope, "volume free space"))
        days, limit = min(limits, default=(None, ""), key=lambda item: item[0] if item[0] is not None else float("inf"))
        confidence = (
            "High"
            if span_days >= 14 and r_squared >= 0.8
            else "Medium"
            if span_days >= 7 and r_squared >= 0.5
            else "Low"
        )
        result[key] = {
            "status": "Growing" if slope > 0 else "Stable",
            "confidence": confidence,
            "growth_mb_per_day": round(slope, 2),
            "days_to_limit": round(days, 1) if days is not None and days >= 0 else None,
            "limit": limit,
            "r_squared": round(r_squared, 3),
        }
    return result


def forecast_text(forecast: dict[str, Any] | None) -> str:
    if not forecast or forecast["status"] == "Insufficient data":
        return "Collecting baseline"
    rate = float(forecast["growth_mb_per_day"])
    if rate <= 0:
        return f"Stable · {forecast['confidence']} confidence"
    days = forecast.get("days_to_limit")
    if days is None:
        return f"+{rate:,.1f} MB/day · no configured limit"
    return (
        f"{days:,.0f} days to {forecast['limit']} · "
        f"{forecast['confidence']} confidence"
    )


def tempdb_balance(files: list[dict[str, Any]], database_name: str) -> dict[str, Any] | None:
    if database_name.lower() != "tempdb":
        return None
    data_files = [item for item in files if item.get("file_type") == "ROWS"]
    if not data_files:
        return {"status": "Unavailable", "message": "No TempDB data files were visible."}
    sizes = [float(item.get("allocated_mb", 0)) for item in data_files]
    growth = [(bool(item.get("growth_is_percent")), float(item.get("growth_value", 0))) for item in data_files]
    balanced_size = min(sizes) > 0 and max(sizes) / min(sizes) <= 1.1
    balanced_growth = len(set(growth)) == 1
    if balanced_size and balanced_growth:
        return {
            "status": "Healthy",
            "message": f"{len(data_files)} TempDB data file(s) have balanced size and growth settings.",
        }
    reasons = []
    if not balanced_size:
        reasons.append("data-file sizes differ by more than 10%")
    if not balanced_growth:
        reasons.append("autogrowth settings are inconsistent")
    return {"status": "Attention", "message": "TempDB " + " and ".join(reasons) + "."}
