from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from .scheduler import configured_timezone

IGNORED_WAITS = {"BROKER_EVENTHANDLER", "BROKER_RECEIVE_WAITFOR", "CHECKPOINT_QUEUE", "LAZYWRITER_SLEEP", "LOGMGR_QUEUE", "REQUEST_FOR_DEADLOCK_SEARCH", "SLEEP_TASK", "SQLTRACE_BUFFER_FLUSH", "WAITFOR", "XE_DISPATCHER_WAIT", "XE_TIMER_EVENT"}


def wait_category(wait_type: str) -> str:
    name = wait_type.upper()
    if name.startswith(("LCK_", "LOCK_HASH")):
        return "Blocking"
    if name.startswith(("PAGEIOLATCH_", "IO_COMPLETION", "ASYNC_IO_COMPLETION")):
        return "Storage I/O"
    if name.startswith(("WRITELOG", "LOGBUFFER")):
        return "Transaction log"
    if name.startswith(("RESOURCE_SEMAPHORE", "CMEMTHREAD")):
        return "Memory"
    if name.startswith(("CXPACKET", "CXCONSUMER", "CXSYNC_")):
        return "Parallelism"
    if name.startswith(("SOS_SCHEDULER_YIELD", "THREADPOOL")):
        return "CPU / workers"
    if name.startswith(("ASYNC_NETWORK_IO", "NET_WAITFOR_PACKET")):
        return "Network / client"
    return "Other"


def counter_delta(current: float, previous: float | None) -> float:
    """Return a non-negative delta and tolerate SQL restarts/counter resets."""
    if previous is None or current < float(previous):
        return 0.0
    return current - float(previous)


def io_delta(current: list[dict[str, Any]], previous: dict[str, Any]) -> list[dict[str, Any]]:
    old = {str(item.get("key")): item for item in previous.get("io_counters", [])}
    result = []
    for item in current:
        prior = old.get(str(item["key"]), {})
        reads = counter_delta(float(item["reads"]), prior.get("reads"))
        writes = counter_delta(float(item["writes"]), prior.get("writes"))
        read_stall = counter_delta(float(item["read_stall_ms"]), prior.get("read_stall_ms"))
        write_stall = counter_delta(float(item["write_stall_ms"]), prior.get("write_stall_ms"))
        result.append({**item, "read_latency_ms": round(read_stall / reads, 2) if reads else None,
                       "write_latency_ms": round(write_stall / writes, 2) if writes else None,
                       "read_mb_delta": round(counter_delta(float(item["bytes_read"]), prior.get("bytes_read")) / 1048576, 2),
                       "write_mb_delta": round(counter_delta(float(item["bytes_written"]), prior.get("bytes_written")) / 1048576, 2)})
    return result


def wait_deltas(current: list[dict[str, Any]], previous: dict[str, Any]) -> list[dict[str, Any]]:
    old = {str(item.get("wait_type")): item for item in previous.get("wait_counters", [])}
    grouped: dict[str, dict[str, Any]] = {}
    for item in current:
        wait_type = str(item["wait_type"])
        if wait_type in IGNORED_WAITS:
            continue
        wait_ms = counter_delta(float(item["wait_ms"]), old.get(wait_type, {}).get("wait_ms"))
        tasks = counter_delta(float(item["tasks"]), old.get(wait_type, {}).get("tasks"))
        if wait_ms <= 0:
            continue
        category = wait_category(wait_type)
        bucket = grouped.setdefault(category, {"category": category, "wait_ms": 0.0, "tasks": 0.0, "top_wait": wait_type, "top_wait_ms": 0.0})
        bucket["wait_ms"] += wait_ms
        bucket["tasks"] += tasks
        if wait_ms > bucket["top_wait_ms"]:
            bucket.update({"top_wait": wait_type, "top_wait_ms": wait_ms})
    return sorted(({**item, "wait_ms": round(item["wait_ms"], 2), "tasks": int(item["tasks"])} for item in grouped.values()), key=lambda item: item["wait_ms"], reverse=True)


def percentile(values: list[float], percent: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    position = (len(clean) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(clean[lower], 2)
    return round(clean[lower] + (clean[upper] - clean[lower]) * (position - lower), 2)


def performance_baselines(rows: list[Any]) -> dict[str, dict[str, float | int | None]]:
    metrics: dict[str, list[float]] = {
        "user_connections": [], "active_sessions": [], "sql_cpu_percent": [],
        "memory_grants_pending": [], "blocked_sessions": [], "longest_blocked_seconds": [],
        "io_read_latency_ms": [], "io_write_latency_ms": [],
    }
    for row in rows:
        item = dict(row)
        details = item.get("details", {})
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except (TypeError, json.JSONDecodeError):
                details = {}
        for metric in tuple(metrics)[:6]:
            value = item.get(metric)
            if value is not None:
                metrics[metric].append(float(value))
        for io_item in details.get("io", []):
            if io_item.get("read_latency_ms") is not None:
                metrics["io_read_latency_ms"].append(float(io_item["read_latency_ms"]))
            if io_item.get("write_latency_ms") is not None:
                metrics["io_write_latency_ms"].append(float(io_item["write_latency_ms"]))
    return {name: {"samples": len(values), "median": percentile(values, 50),
                   "p95": percentile(values, 95), "p99": percentile(values, 99),
                   "peak": round(max(values), 2) if values else None}
            for name, values in metrics.items()}


def period_baseline_comparison(
    rows: list[Any], current: dict[str, Any], timezone_name: str = "UTC"
) -> dict[str, Any]:
    """Compare the current sample with the same local hour and weekday history."""
    zone = configured_timezone(timezone_name)
    current_time = datetime.fromisoformat(str(current["collected_at"])).astimezone(zone)
    same_hour = []
    same_weekday = []
    for row in rows:
        item = dict(row)
        try:
            observed = datetime.fromisoformat(str(item["collected_at"])).astimezone(zone)
        except (TypeError, ValueError):
            continue
        if observed.hour == current_time.hour:
            same_hour.append(item)
        if observed.weekday() == current_time.weekday():
            same_weekday.append(item)
    current_cpu = float(current.get("sql_cpu_percent") or 0)
    hour = performance_baselines(same_hour)["sql_cpu_percent"]
    weekday = performance_baselines(same_weekday)["sql_cpu_percent"]
    return {
        "timezone": str(zone), "local_hour": current_time.hour,
        "weekday": current_time.strftime("%A"), "same_hour": hour, "same_weekday": weekday,
        "cpu_vs_hour_p95": None if hour["p95"] is None else round(current_cpu - float(hour["p95"]), 2),
        "cpu_vs_weekday_p95": None if weekday["p95"] is None else round(current_cpu - float(weekday["p95"]), 2),
    }


def sustained_performance_assessment(
    rows: list[Any], current: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    baselines = performance_baselines(rows)
    io_read = max((float(item["read_latency_ms"]) for item in current.get("io", [])
                   if item.get("read_latency_ms") is not None), default=0.0)
    io_write = max((float(item["write_latency_ms"]) for item in current.get("io", [])
                    if item.get("write_latency_ms") is not None), default=0.0)
    values = {
        "sql_cpu_percent": float(current.get("sql_cpu_percent") or 0),
        "memory_grants_pending": float(current.get("memory_grants_pending") or 0),
        "longest_blocked_seconds": float(current.get("longest_blocked_seconds") or 0),
        "io_read_latency_ms": io_read, "io_write_latency_ms": io_write,
    }
    limits = {
        "sql_cpu_percent": float(policy.get("performance_cpu_percent", 80)),
        "memory_grants_pending": float(policy.get("performance_grants_pending", 1)),
        "longest_blocked_seconds": float(policy.get("performance_blocked_seconds", 60)),
        "io_read_latency_ms": float(policy.get("performance_io_latency_ms", 20)),
        "io_write_latency_ms": float(policy.get("performance_io_latency_ms", 20)),
    }
    sustain_minutes = max(1, int(policy.get("performance_sustain_minutes", 10)))
    current_at = datetime.fromisoformat(str(current["collected_at"]))
    breaches = []
    for metric, value in values.items():
        baseline = baselines[metric]
        adaptive = float(baseline["p95"] or 0) if int(baseline["samples"] or 0) >= 5 else 0
        threshold = max(limits[metric], adaptive)
        if value < threshold or value <= 0:
            continue
        first_at = current_at
        consecutive = 1
        for row in reversed(rows[:-1]):
            historical = dict(row)
            if metric.startswith("io_"):
                try:
                    detail = json.loads(historical.get("details") or "{}")
                except (TypeError, json.JSONDecodeError):
                    detail = {}
                key = "read_latency_ms" if metric == "io_read_latency_ms" else "write_latency_ms"
                historical_value = max((float(item[key]) for item in detail.get("io", [])
                                        if item.get(key) is not None), default=0.0)
            else:
                historical_value = float(historical.get(metric) or 0)
            if historical_value < threshold:
                break
            first_at = datetime.fromisoformat(str(historical["collected_at"]))
            consecutive += 1
        duration_minutes = max(0.0, (current_at - first_at).total_seconds() / 60)
        breaches.append({"metric": metric, "value": round(value, 2),
                         "threshold": round(threshold, 2), "fixed_threshold": limits[metric],
                         "consecutive_samples": consecutive,
                         "duration_minutes": round(duration_minutes, 2),
                         "sustained": duration_minutes >= sustain_minutes})
    return {"baselines": baselines, "breaches": breaches,
            "sustained": [item for item in breaches if item["sustained"]],
            "sustain_minutes": sustain_minutes}
