from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from statistics import median
from typing import Any
from urllib.parse import urlsplit, urlunsplit

BACKUP_TYPES = {"D": "Full", "I": "Differential", "L": "Transaction log"}


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def sanitize_destination(value: Any) -> str:
    """Keep useful backup evidence while removing URL credentials and query signatures."""
    text = str(value or "")
    if "://" not in text:
        return text
    parts = urlsplit(text)
    hostname = parts.hostname or ""
    if parts.port:
        hostname += f":{parts.port}"
    return urlunsplit((parts.scheme, hostname, parts.path, "", ""))


def backup_baselines(rows: list[dict[str, Any]], minimum_samples: int = 5) -> dict[str, dict[str, Any]]:
    """Build robust per-type duration/size baselines and label meaningful outliers."""
    result: dict[str, dict[str, Any]] = {}
    for code in BACKUP_TYPES:
        typed = []
        for row in rows:
            if str(row.get("backup_type")) != code:
                continue
            start, finish = _aware(row.get("start_time")), _aware(row.get("finish_time"))
            duration = max(0.0, (finish - start).total_seconds()) if start and finish else None
            try:
                size = float(row.get("backup_size_mb"))
            except (TypeError, ValueError):
                size = None
            if duration is not None and size is not None:
                typed.append((row, duration, size))
        if len(typed) < minimum_samples:
            result[code] = {"sample_count": len(typed), "confidence": "Insufficient history", "anomalies": []}
            continue
        duration_med = median(item[1] for item in typed)
        size_med = median(item[2] for item in typed)
        anomalies = []
        for row, duration, size in typed:
            reasons = []
            if duration_med > 0 and duration >= duration_med * 2:
                reasons.append("Duration is at least 2× baseline")
            if size_med > 0 and size <= size_med * 0.5:
                reasons.append("Size is at most 50% of baseline")
            elif size_med > 0 and size >= size_med * 1.5:
                reasons.append("Size is at least 150% of baseline")
            if reasons:
                anomalies.append({"backup_set_id": row.get("backup_set_id"), "finish_time": str(row.get("finish_time", "")), "reasons": reasons})
        result[code] = {
            "sample_count": len(typed), "confidence": "Established",
            "median_duration_seconds": round(duration_med, 2),
            "median_size_mb": round(size_med, 2), "anomalies": anomalies,
        }
    return result


def compliance_calendar(
    rows: list[dict[str, Any]], *, now: datetime | None = None,
    days: int = 31, require_full: bool = True,
) -> list[dict[str, Any]]:
    """Summarize daily SQL-native backup evidence for a compact client calendar."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        finish = _aware(row.get("finish_time"))
        if finish:
            by_day.setdefault(finish.date().isoformat(), []).append(row)
    result = []
    for offset in range(days - 1, -1, -1):
        day = (now.date() - timedelta(days=offset)).isoformat()
        evidence = by_day.get(day, [])
        types = sorted({str(row.get("backup_type")) for row in evidence})
        status = "No evidence" if not evidence else "Attention" if require_full and "D" not in types else "Compliant"
        result.append({"day": day, "status": status, "types": types, "count": len(evidence)})
    return result


def recovery_readiness(
    assurance: dict[str, Any], evidence: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    successes = [item for item in evidence if str(item.get("result")) == "Succeeded"]
    latest = max(successes, key=lambda item: _aware(item.get("performed_at")) or datetime.min.replace(tzinfo=UTC), default=None)
    performed = _aware(latest.get("performed_at")) if latest else None
    evidence_age = (now - performed).days if performed else None
    reasons = []
    if assurance.get("status") != "Healthy": reasons.append("Backup SLA needs attention")
    if assurance.get("chain_status") == "Gap detected": reasons.append("Transaction-log chain gap detected")
    if not latest: reasons.append("No successful verification or restore-test evidence")
    elif evidence_age is not None and evidence_age > 90: reasons.append("Recovery evidence is older than 90 days")
    restore = next((item for item in successes if item.get("evidence_type") == "Isolated restore test"), None)
    if not restore: reasons.append("No successful isolated restore test recorded")
    return {
        "status": "Ready" if not reasons else "Attention",
        "reasons": reasons, "latest_success": latest,
        "evidence_age_days": evidence_age,
    }


def assess_backup_history(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    policy: dict[str, Any] | None = None,
    recovery_model: str = "FULL",
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    policy = policy or {}
    require_log = policy.get("require_log")
    if require_log is None:
        require_log = recovery_model.upper() != "SIMPLE"
    rules = {
        "D": (bool(policy.get("require_full", True)), float(policy.get("full_max_hours", 24))),
        "I": (bool(policy.get("require_differential", False)), float(policy.get("differential_max_hours", 24))),
        "L": (bool(require_log), float(policy.get("log_max_minutes", 60)) / 60.0),
    }
    ordered = sorted(rows, key=lambda row: _aware(row.get("finish_time")) or datetime.min.replace(tzinfo=UTC), reverse=True)
    baselines = backup_baselines(rows)
    checks = []
    for code, (required, maximum) in rules.items():
        latest = next((row for row in ordered if str(row.get("backup_type")) == code), None)
        finished = _aware(latest.get("finish_time")) if latest else None
        age = max(0.0, (now - finished).total_seconds() / 3600) if finished else None
        status = "Not required" if not required else "Missing" if age is None else "Compliant" if age <= maximum else "Late"
        baseline = baselines.get(code, {})
        latest_anomaly = next((item for item in baseline.get("anomalies", []) if latest and item.get("backup_set_id") == latest.get("backup_set_id")), None)
        checks.append({
            "backup_type": code,
            "label": BACKUP_TYPES[code],
            "required": required,
            "status": status,
            "latest": finished.isoformat(timespec="seconds") if finished else "",
            "age_hours": round(age, 2) if age is not None else None,
            "maximum_hours": maximum,
            "destination": sanitize_destination(latest.get("destination")) if latest else "",
            "has_checksums": bool(latest.get("has_checksums")) if latest else False,
            "baseline": baseline.get("confidence", "Insufficient history"),
            "baseline_anomaly": "; ".join(latest_anomaly.get("reasons", [])) if latest_anomaly else "",
            "median_duration_seconds": baseline.get("median_duration_seconds"),
            "median_size_mb": baseline.get("median_size_mb"),
        })
    logs = sorted((row for row in rows if str(row.get("backup_type")) == "L"), key=lambda row: _aware(row.get("finish_time")) or datetime.min.replace(tzinfo=UTC))
    gaps = []
    for previous, current in pairwise(logs):
        try:
            if float(current.get("first_lsn")) > float(previous.get("last_lsn")):
                gaps.append(f"Gap before backup set {current.get('backup_set_id', 'unknown')}")
        except (TypeError, ValueError):
            continue
    required_checks = [item for item in checks if item["required"]]
    rpo = next((item["age_hours"] for item in checks if item["backup_type"] == "L"), None)
    if recovery_model.upper() == "SIMPLE":
        rpo = next((item["age_hours"] for item in checks if item["backup_type"] == "D"), None)
    anomalies = sum(len(item.get("anomalies", [])) for item in baselines.values())
    return {
        "status": "Healthy" if all(item["status"] == "Compliant" for item in required_checks) and not gaps else "Attention",
        "recovery_model": recovery_model,
        "checks": checks,
        "chain_status": "Gap detected" if gaps else "Continuous" if logs else "No log history",
        "chain_gaps": gaps,
        "rpo_exposure_hours": rpo,
        "history_count": len(rows),
        "baselines": baselines,
        "anomaly_count": anomalies,
        "calendar": compliance_calendar(rows, now=now, require_full=bool(policy.get("require_full", True))),
    }
