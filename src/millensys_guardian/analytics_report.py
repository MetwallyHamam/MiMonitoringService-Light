from __future__ import annotations

import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from statistics import median
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from matplotlib.patches import FancyBboxPatch, Wedge
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .sql_jobs import job_intelligence
from .sql_performance import performance_baselines
from .store import Store
from .system_info import server_identity
from .time_utils import parse_timestamp, server_time_text, server_timezone

NAVY = "#101c35"
TEAL = "#20aebe"
BLUE = "#2563eb"
GREEN = "#16a34a"
AMBER = "#f59e0b"
RED = "#dc2626"
PALE = "#f3f6fb"


@dataclass(frozen=True)
class ReportPeriod:
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        return f"{self.start:%d %b %Y} - {self.end:%d %b %Y}"


def this_month(store: Store, now: datetime | None = None) -> ReportPeriod:
    config = store.get("configuration")
    zone = server_timezone(config)
    current = (now or datetime.now(UTC)).astimezone(zone)
    return ReportPeriod(
        datetime.combine(current.date().replace(day=1), time.min, zone),
        datetime.combine(current.date(), time.max, zone),
    )


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _availability(events: list[dict[str, Any]], target: str, category: str, period: ReportPeriod) -> float:
    relevant = [e for e in events if e["target"] == target and e["category"] == category]
    relevant.sort(key=lambda e: e["created_at"])
    down_at: datetime | None = None
    down_seconds = 0.0
    for event in relevant:
        when = _parse(event["created_at"])
        if event["severity"] in {"critical", "error"} and down_at is None:
            down_at = when
        elif event["severity"] == "recovery" and down_at is not None:
            down_seconds += max(0.0, (when - down_at).total_seconds())
            down_at = None
    if down_at is not None:
        down_seconds += max(0.0, (period.end.astimezone(UTC) - down_at).total_seconds())
    total = max(1.0, (period.end - period.start).total_seconds())
    return max(0.0, 100.0 * (1.0 - min(down_seconds, total) / total))


def build_analytics(store: Store, period: ReportPeriod) -> dict[str, Any]:
    since, until = _utc(period.start), _utc(period.end + timedelta(microseconds=1))
    config = store.get("configuration")
    zone = server_timezone(config)
    profiles = {str(item.get("id") or item.get("server")): item
                for item in config.get("sql_monitors", [])}
    with store.connect() as db:
        events = [dict(row) for row in db.execute(
            "SELECT * FROM events WHERE created_at>=? AND created_at<? ORDER BY created_at", (since, until)
        )]
        health = [dict(row) for row in db.execute(
            "SELECT * FROM health_samples WHERE created_at>=? AND created_at<? ORDER BY created_at", (since, until)
        )]
    sql = [dict(row) for row in store.sql_samples(since, until)]
    backups = [dict(row) for row in store.backup_samples(since, until)]
    sql_backups = [dict(row) for row in store.sql_backup_history(since) if row["finish_time"] < until]
    recovery_evidence = [dict(row) for row in store.sql_recovery_evidence() if since <= row["performed_at"] < until]
    job_rows = [dict(row) for row in store.sql_job_history() if row["run_at"] and since <= row["run_at"] < until]
    performance_rows = [dict(row) for row in store.sql_performance_samples(since) if row["collected_at"] < until]
    query_evidence = [dict(row) for row in store.sql_query_evidence(since, limit=5000) if row["collected_at"] < until]
    for event in events:
        event["display_created_at"] = server_time_text(event.get("created_at"), config, "%Y-%m-%d %H:%M")
    performance_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in performance_rows:
        performance_grouped[row["profile_id"]].append(row)
    performance_summary = []
    for profile_id, rows in performance_grouped.items():
        baseline = performance_baselines(rows)
        profile = profiles.get(str(profile_id), {})
        instance_name = str(profile.get("name") or profile.get("server") or profile_id)
        performance_summary.append({"profile_id": profile_id, "instance_name": instance_name,
                                    "samples": len(rows),
                                    "cpu_samples": int(baseline["sql_cpu_percent"]["samples"] or 0),
                                    "cpu": baseline["sql_cpu_percent"],
                                    "sessions": baseline["active_sessions"],
                                    "blocking_peak_seconds": max(int(row["longest_blocked_seconds"]) for row in rows),
                                    "grants_peak": max(int(row["memory_grants_pending"]) for row in rows)})
    active_by_signature: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in query_evidence:
        if row["evidence_type"] != "long_running":
            continue
        key = (
            str(row["profile_id"]),
            str(row["database_name"] or ""),
            str(row["query_signature"]),
            str(row.get("source_name") or ""),
        )
        previous = active_by_signature.get(key)
        if previous is None or int(row["duration_ms"]) > int(previous["duration_ms"]):
            active_by_signature[key] = row
    top_queries = sorted(
        active_by_signature.values(), key=lambda row: int(row["duration_ms"]), reverse=True
    )[:20]
    jobs_grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in job_rows:
        jobs_grouped[(row["profile_id"], row["job_name"])].append(row)
    job_summary = []
    for (profile_id, job_name), rows in jobs_grouped.items():
        intelligence = job_intelligence(rows, now=period.end)
        job_summary.append({"profile_id": profile_id, "job_name": job_name, **intelligence})
    agent_executions = sum(item["executions"] for item in job_summary)
    agent_success_percent = 100 * sum((item["success_percent"] or 0) * item["executions"] / 100 for item in job_summary) / max(1, agent_executions)
    incidents = [e for e in events if e["severity"] in {"critical", "error", "warning"}]
    recoveries = [e for e in events if e["severity"] == "recovery"]
    thresholds = config.get("performance", {})
    disk_minimum = float(config.get("disk", {}).get("minimum_free_percent", 15))
    health_points = []
    for row in health:
        score = 100.0
        score -= max(0.0, float(row["cpu"]) - float(thresholds.get("cpu_percent", 90))) * 0.8
        score -= max(0.0, float(row["memory"]) - float(thresholds.get("memory_percent", 90))) * 0.8
        disk = row["minimum_disk_free_percent"]
        if disk is not None and float(disk) < disk_minimum:
            score -= min(20.0, disk_minimum - float(disk))
        health_points.append((row["created_at"], max(0.0, round(score, 1))))
    databases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sql:
        databases[f"{row['profile_name']} / {row['database_name']}"].append(row)
    database_summary = []
    for name, rows in databases.items():
        start_mb, end_mb = float(rows[0]["total_mb"]), float(rows[-1]["total_mb"])
        online = sum(1 for row in rows if str(row["state"]).upper() == "ONLINE")
        database_summary.append({
            "name": name, "start_mb": start_mb, "end_mb": end_mb,
            "growth_mb": end_mb - start_mb,
            "healthy_percent": 100.0 * online / len(rows), "samples": rows,
        })
    backup_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in backups:
        backup_by_day[parse_timestamp(row["checked_at"]).astimezone(zone).date().isoformat()].append(row)
    backup_days = []
    day = period.start.date()
    while day <= period.end.date():
        rows = backup_by_day.get(day.isoformat(), [])
        backup_days.append({
            "date": day.isoformat(), "ok": bool(rows) and all(row["ok"] for row in rows),
            "checked": bool(rows), "count": len(rows),
        })
        day += timedelta(days=1)
    service_targets = [dict(row) for row in store.targets("service") if row["enabled"]]
    pool_targets = [dict(row) for row in store.targets("pool") if row["enabled"]]
    services = [{
        "name": row["display_name"], "key": row["target_key"],
        "availability": _availability(events, row["target_key"], "service", period),
        "incidents": sum(1 for event in incidents if event["target"] == row["target_key"]),
    } for row in service_targets]
    pools = [{
        "name": row["display_name"], "key": row["target_key"],
        "availability": _availability(events, row["target_key"], "pool", period),
        "incidents": sum(1 for event in incidents if event["target"] == row["target_key"]),
    } for row in pool_targets]
    backup_pass = sum(1 for item in backups if item["ok"])
    backup_rate = 100.0 * backup_pass / len(backups) if backups else 0.0
    average_health = sum(point[1] for point in health_points) / len(health_points) if health_points else 0.0
    stability = services + pools
    average_availability = (
        sum(item["availability"] for item in stability) / len(stability) if stability else 100.0
    )
    impact = []
    if incidents:
        impact.append(f"{len(incidents)} operational warning or incident event(s) required attention.")
    else:
        impact.append("No monitored operational incident was recorded in the selected period.")
    impact.append(f"Monitored service and pool availability averaged {average_availability:.2f}%.")
    if backups:
        impact.append(f"Database backup validations passed {backup_rate:.1f}% of recorded checks.")
    else:
        impact.append("No database backup validation result was recorded in this period.")
    if sql_backups:
        impact.append(f"SQL Server recorded {len(sql_backups)} native full, differential, or log backup set(s).")
    successful_restore = [item for item in recovery_evidence if item["result"] == "Succeeded" and item["evidence_type"] == "Isolated restore test"]
    if successful_restore:
        impact.append("A successful isolated restore test provides direct recovery-readiness evidence.")
    if job_summary:
        total_executions = sum(item["executions"] for item in job_summary)
        total_success = sum(item["success_percent"] * item["executions"] / 100 for item in job_summary if item["success_percent"] is not None)
        impact.append(f"SQL Agent completed {total_executions} monitored execution(s) with {100 * total_success / max(1, total_executions):.1f}% success.")
    cpu_sample_count = sum(item["cpu_samples"] for item in performance_summary)
    if cpu_sample_count:
        peak_cpu = max(float(item["cpu"]["peak"] or 0) for item in performance_summary)
        impact.append(f"SQL Server process CPU peaked at {peak_cpu:.1f}% across {cpu_sample_count} corrected CPU sample(s).")
    elif performance_summary:
        impact.append("Earlier inverted SQL CPU values were invalidated; corrected CPU history begins after this upgrade.")
    performance_findings = []
    cpu_limit = 80.0
    for item in performance_summary:
        if not item["cpu_samples"]:
            continue
        p95 = float(item["cpu"]["p95"] or 0)
        if p95 < cpu_limit:
            continue
        candidates = [row for row in top_queries if row["profile_id"] == item["profile_id"]]
        if candidates and int(candidates[0]["cpu_ms"] or 0) >= 1000:
            query = candidates[0]
            evidence = str(query["query_signature"] or "unknown")[-12:]
            database = query["database_name"] or "unidentified database"
            cause = f"Long-running request evidence {evidence} was observed on {database}; verify its timing against the CPU breach."
        else:
            cause = "No dominant query contributor was captured; compare host CPU and collect another performance sample."
        performance_findings.append({"instance": item["instance_name"],
                                     "breach": f"SQL CPU P95 {p95:.1f}% (threshold {cpu_limit:.0f}%)",
                                     "likely_cause": cause,
                                     "evidence": f"{item['samples']} samples; peak {float(item['cpu']['peak'] or 0):.1f}%"})
    actions = []
    if any(not item["ok"] for item in backups):
        actions.append("Investigate failed or overdue database backups and confirm restore readiness.")
    if sql_backups and not successful_restore:
        actions.append("Schedule and record an isolated restore test to prove recoverability, not only backup completion.")
    if any((item["success_percent"] or 0) < 100 or item["abnormal_duration"] for item in job_summary):
        actions.append("Review failed or abnormally long SQL Agent jobs and resolve the recorded step-level cause.")
    if any(item["availability"] < 99.9 for item in stability):
        actions.append("Review unstable services or IIS pools and remove recurring failure causes.")
    if any(item["healthy_percent"] < 100 for item in database_summary):
        actions.append("Review database availability samples that were not ONLINE.")
    if any(item["growth_mb"] > 0 for item in database_summary):
        actions.append("Validate database capacity forecasts against observed allocation growth.")
    if any(float(item["cpu"]["p95"] or 0) >= 80 or item["blocking_peak_seconds"] >= 60 for item in performance_summary):
        actions.append("Review sustained SQL CPU or blocking evidence and the ranked query signatures contributing most resource use.")
    if not actions:
        actions.append("Continue routine monitoring and retain the current preventive maintenance schedule.")
    identity = server_identity()
    return {
        "period": period, "client": config.get("client", {}), "server": identity.server_name,
        "generated_at": datetime.now(UTC), "events": events, "incidents": incidents,
        "recoveries": recoveries, "health_points": health_points, "health_samples": health,
        "average_health": average_health, "performance_thresholds": thresholds,
        "disk_minimum": disk_minimum,
        "databases": database_summary, "backup_days": backup_days, "backups": backups,
        "sql_backups": sql_backups, "recovery_evidence": recovery_evidence,
        "job_summary": job_summary,
        "performance_summary": performance_summary, "query_evidence": query_evidence,
        "top_queries": top_queries, "performance_findings": performance_findings,
        "agent_success_percent": agent_success_percent,
        "backup_rate": backup_rate, "services": services, "pools": pools,
        "availability": average_availability, "business_impact": impact, "next_actions": actions,
        "severity_counts": Counter(event["severity"] for event in events),
    }


def _dashboard_image(report: dict[str, Any], destination: Path) -> None:
    fig = plt.figure(figsize=(16, 9), facecolor="#f4f7fb")
    client = report["client"].get("name") or "Client"
    fig.text(.025, .965, "Infrastructure Health and Stability Dashboard", fontsize=22,
             fontweight="bold", color="#172033", va="top")
    fig.text(.025, .915, f"{client}  |  {report['server']}  |  {report['period'].label}",
             fontsize=8.5, color="#64748b")
    fig.text(.975, .965, "MILLENSYS IT OPERATIONS", fontsize=8, color="#64748b",
             ha="right", va="top")
    fig.add_artist(plt.Line2D([.025, .975], [.895, .895], transform=fig.transFigure,
                             color="#20aebe", linewidth=3))
    health = report["health_samples"]
    thresholds = report["performance_thresholds"]
    cpu_limit = float(thresholds.get("cpu_percent", 90))
    memory_limit = float(thresholds.get("memory_percent", 90))
    cpu_breaches = sum(float(row["cpu"]) >= cpu_limit for row in health)
    memory_breaches = sum(float(row["memory"]) >= memory_limit for row in health)
    disk_breaches = sum(row["minimum_disk_free_percent"] is not None and
                        float(row["minimum_disk_free_percent"]) < report["disk_minimum"] for row in health)
    failed_backup_days = sum(item["checked"] and not item["ok"] for item in report["backup_days"])
    growth_mb = sum(float(item["growth_mb"]) for item in report["databases"])
    kpis = [
        ("Overall health", f"{report['average_health']:.0f}/100", "Stable" if report["average_health"] >= 95 else "Attention", GREEN),
        ("CPU breaches", str(cpu_breaches), "threshold violations", GREEN if not cpu_breaches else RED),
        ("Memory breaches", str(memory_breaches), "threshold violations", GREEN if not memory_breaches else RED),
        ("Disk breaches", str(disk_breaches), "low-space samples", GREEN if not disk_breaches else AMBER),
        ("Backup exceptions", str(failed_backup_days), "days requiring review", GREEN if not failed_backup_days else RED),
        ("Allocated growth", f"{growth_mb:,.1f} MB", "selected period", BLUE),
    ]
    gap, left, total_width = .009, .025, .95
    width = (total_width - gap * 5) / 6
    for index, (label, value, note, accent) in enumerate(kpis):
        x = left + index * (width + gap)
        fig.add_artist(FancyBboxPatch((x, .80), width, .085, transform=fig.transFigure,
                                      boxstyle="round,pad=0.006,rounding_size=.006",
                                      facecolor="white", edgecolor="#dbe3ec", linewidth=1))
        fig.add_artist(plt.Line2D([x, x], [.80, .885], transform=fig.transFigure,
                                 color=accent, linewidth=4))
        fig.text(x + .011, .866, label, fontsize=8, color="#64748b", va="top")
        fig.text(x + .011, .846, value, fontsize=15, color="#172033", fontweight="bold", va="top")
        fig.text(x + .011, .807, note, fontsize=6.2, color="#64748b", va="bottom")

    def panel(bounds: list[float], title: str):
        ax = fig.add_axes(bounds, facecolor="white")
        for spine in ax.spines.values(): spine.set_edgecolor("#dbe3ec")
        ax.set_title(title, loc="left", fontsize=11, fontweight="bold", color="#172033", pad=10)
        ax.tick_params(colors="#64748b", labelsize=7)
        ax.grid(color="#e8edf3", linewidth=.7, alpha=.9)
        return ax

    utilization = panel([.04, .50, .44, .245], "Infrastructure utilization trend")
    if health:
        bucket_size = max(1, (len(health) + 119) // 120)
        trend = []
        for offset in range(0, len(health), bucket_size):
            bucket = health[offset:offset + bucket_size]
            disk = [float(row["minimum_disk_free_percent"]) for row in bucket
                    if row["minimum_disk_free_percent"] is not None]
            trend.append({
                "time": _parse(bucket[len(bucket) // 2]["created_at"]),
                "cpu": median(float(row["cpu"]) for row in bucket),
                "memory": median(float(row["memory"]) for row in bucket),
                "disk": median(disk) if disk else float("nan"),
            })
        x = [row["time"] for row in trend]
        utilization.plot(x, [row["cpu"] for row in trend], color=BLUE,
                         linewidth=1.0, label="CPU", solid_capstyle="round")
        utilization.plot(x, [row["memory"] for row in trend], color=RED,
                         linewidth=1.0, label="Memory", solid_capstyle="round")
        utilization.plot(x, [row["disk"] for row in trend], color=AMBER,
                         linewidth=.9, linestyle="--", label="Disk free")
        utilization.axhline(cpu_limit, color="#94a3b8", linewidth=.8,
                            linestyle=(0, (4, 4)), alpha=.75, label="CPU threshold")
        utilization.set_ylim(0, 105)
        locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
        utilization.xaxis.set_major_locator(locator)
        utilization.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        utilization.legend(loc="upper left", frameon=False, ncol=4, fontsize=6.5,
                           handlelength=2.2, columnspacing=1.2)
    else:
        utilization.text(.5, .5, "No utilization samples", transform=utilization.transAxes, ha="center", color="#64748b")

    growth = panel([.52, .50, .44, .245], "Allocated database growth")
    by_time: dict[str, float] = defaultdict(float)
    for database in report["databases"]:
        for sample in database["samples"]:
            by_time[str(sample["created_at"])] += float(sample["total_mb"])
    if by_time:
        ordered = sorted(by_time.items()); gx = [_parse(item[0]) for item in ordered]; gy = [item[1] for item in ordered]
        growth.plot(gx, gy, color=BLUE, linewidth=2.5, marker="o", markersize=3)
        growth.fill_between(gx, gy, min(gy), color=BLUE, alpha=.16)
        growth.xaxis.set_major_formatter(mdates.DateFormatter("%d %b")); growth.set_ylabel("Allocated MB", fontsize=7)
    else:
        growth.text(.5, .5, "No database capacity samples", transform=growth.transAxes, ha="center", color="#64748b")

    breach = panel([.04, .18, .27, .24], "Threshold breach analysis")
    breach_names = ["CPU", "Memory", "Disk", "Backup"]
    breach_values = [cpu_breaches, memory_breaches, disk_breaches, failed_backup_days]
    breach.barh(breach_names, breach_values, color=[BLUE, RED, AMBER, GREEN], height=.42)
    breach.invert_yaxis(); breach.set_xlim(0, max(1, max(breach_values) * 1.2))
    for idx, value in enumerate(breach_values): breach.text(value + .02, idx, str(value), va="center", fontsize=8, color="#334155")

    donut = panel([.35, .18, .18, .24], "Backup compliance")
    rate = max(0, min(100, report["backup_rate"]))
    donut.pie([rate, 100 - rate], colors=[GREEN, "#e5eaf0"], startangle=90,
              wedgeprops={"width": .30, "edgecolor": "white"})
    donut.text(0, .05, f"{rate:.0f}%", ha="center", va="center", fontsize=20, fontweight="bold", color="#172033")
    donut.text(0, -.23, "compliant", ha="center", va="center", fontsize=7, color="#64748b")

    gauge = panel([.57, .18, .18, .24], "Operational risk")
    gauge.set_xlim(-1.2, 1.2); gauge.set_ylim(-.25, 1.2); gauge.axis("off")
    for start, end, color in ((0, 60, GREEN), (60, 120, AMBER), (120, 180, RED)):
        gauge.add_patch(Wedge((0, 0), 1, start, end, width=.16, facecolor=color, edgecolor="none"))
    risk = max(0, min(100, 100 - report["average_health"] + min(35, len(report["incidents"]) * 3)))
    angle = 180 - 180 * risk / 100
    gauge.plot([0, .82 * __import__("math").cos(__import__("math").radians(angle))],
               [0, .82 * __import__("math").sin(__import__("math").radians(angle))], color="#172033", linewidth=2)
    gauge.scatter([0], [0], s=28, color="#172033"); gauge.text(0, .14, f"{risk:.0f}", ha="center", fontsize=18, fontweight="bold")
    gauge.text(-.9, .78, "Low", fontsize=7, color=GREEN); gauge.text(.78, .78, "High", fontsize=7, color=RED)

    insights = panel([.79, .18, .17, .24], "Executive insights")
    insights.axis("off")
    insight_lines = [
        f"Health averaged {report['average_health']:.1f}/100",
        f"Availability reached {report['availability']:.2f}%",
        f"Backup compliance was {rate:.1f}%",
        f"Database allocation changed {growth_mb:+,.1f} MB",
        f"{len(report['incidents'])} incident or warning event(s)",
    ]
    for index, line in enumerate(insight_lines):
        insights.text(.03, .90 - index * .18, f"-  {line}", transform=insights.transAxes,
                      fontsize=8, color="#334155", va="top")
    fig.text(.025, .055, "Prepared by Millensys Support Department", fontsize=7, color="#64748b")
    fig.text(.975, .055, "Confidential client report", fontsize=7, color="#64748b", ha="right")
    fig.savefig(destination, dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)


def create_charts(report: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "axes.titleweight": "bold", "font.size": 9})
    paths: dict[str, Path] = {}
    paths["dashboard"] = output_dir / "executive-dashboard.png"
    _dashboard_image(report, paths["dashboard"])

    return paths


def _docx_cell_shading(cell, fill: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd"); shading.set(qn("w:fill"), fill.replace("#", "")); properties.append(shading)


def _docx_table(document: Document, headers: list[str], rows: list[list[str]]) -> None:
    table = document.add_table(rows=1, cols=len(headers)); table.style = "Table Grid"
    repeat_header = OxmlElement("w:tblHeader")
    repeat_header.set(qn("w:val"), "true")
    table.rows[0]._tr.get_or_add_trPr().append(repeat_header)
    for index, header in enumerate(headers):
        cell = table.rows[0].cells[index]; cell.text = header; _docx_cell_shading(cell, NAVY)
        cell.paragraphs[0].paragraph_format.keep_with_next = True
        for run in cell.paragraphs[0].runs:
            run.font.color.rgb = RGBColor(255, 255, 255)
            run.font.size = Pt(8.5)
            run.bold = True
    for row_index, values in enumerate(rows):
        row = table.add_row()
        cannot_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(cannot_split)
        cells = row.cells
        for index, value in enumerate(values):
            cells[index].text = str(value)
            for paragraph in cells[index].paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(8.5)
            if row_index % 2: _docx_cell_shading(cells[index], "EEF4F8")
    document.add_paragraph()


def export_docx(store: Store, period: ReportPeriod, destination: Path) -> Path:
    report = build_analytics(store, period)
    with tempfile.TemporaryDirectory(prefix="mi-report-") as folder:
        charts = create_charts(report, Path(folder))
        document = Document(); section = document.sections[0]
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = section.page_height, section.page_width
        section.top_margin = Inches(.45); section.bottom_margin = Inches(.45)
        section.left_margin = Inches(.5); section.right_margin = Inches(.5)
        styles = document.styles
        styles["Normal"].font.name = "Aptos"; styles["Normal"].font.size = Pt(9.5)
        styles["Title"].font.name = "Aptos Display"; styles["Title"].font.size = Pt(30)
        styles["Title"].font.color.rgb = RGBColor(0, 0, 0)
        for level in (1, 2):
            styles[f"Heading {level}"].font.color.rgb = RGBColor(0, 0, 0)
        document.add_picture(str(charts["dashboard"]), width=Inches(10.65))
        document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        document.add_page_break()
        title = document.add_paragraph(style="Title"); title.add_run("Client Infrastructure Analytics Report")
        client = report["client"].get("name") or "Client"
        intro = document.add_paragraph()
        intro.add_run(f"{client}  |  {report['server']}\n").bold = True
        intro.add_run(f"Reporting period {period.label}\nPrepared by Millensys Support Department")
        document.add_paragraph(
            f"This report summarizes monitored infrastructure health, database capacity and backup assurance, incidents, and platform stability. "
            f"Overall server health averaged {report['average_health']:.1f}/100 and monitored availability averaged {report['availability']:.2f}% during the selected period."
        )
        document.add_heading("1 Duration overview summary", level=1)
        _docx_table(document, ["Measure", "Result"], [
            ["Reporting period", period.label], ["Server health average", f"{report['average_health']:.1f}/100"],
            ["Operational incidents and warnings", str(len(report["incidents"]))],
            ["Recorded recoveries", str(len(report["recoveries"]))],
            ["Backup validation pass rate", f"{report['backup_rate']:.1f}%"],
            ["SQL Agent success percentage", f"{report['agent_success_percent']:.1f}%"],
            ["Service and pool availability", f"{report['availability']:.2f}%"],
        ])
        document.add_heading("2 Detailed operational evidence", level=1)
        document.add_paragraph("The dashboard summarizes health, utilization, capacity growth, backup compliance, threshold breaches, and operational risk. The following tables retain the supporting evidence for review and audit.")
        document.add_heading("3 Database health and backup validation", level=1)
        _docx_table(document, ["Database", "Start MB", "End MB", "Growth MB", "ONLINE samples"], [[
            item["name"], f"{item['start_mb']:.1f}", f"{item['end_mb']:.1f}", f"{item['growth_mb']:+.1f}", f"{item['healthy_percent']:.1f}%"
        ] for item in report["databases"]] or [["No database samples", "-", "-", "-", "-"]])
        _docx_table(document, ["Date", "Validation"], [[item["date"], "Passed" if item["ok"] else "Failed" if item["checked"] else "No recorded check"] for item in report["backup_days"]])
        _docx_table(document, ["SQL backup evidence", "Count"], [["Native full/differential/log backup sets", str(len(report["sql_backups"]))], ["Verification and restore-test records", str(len(report["recovery_evidence"]))]])
        _docx_table(document, ["Date", "Database", "Recovery evidence", "Result", "Owner"], [[i["performed_at"][:10], i["database_name"], i["evidence_type"], i["result"], i["owner"]] for i in report["recovery_evidence"]] or [["-", "-", "No recovery evidence in period", "-", "-"]])
        _docx_table(document, ["SQL Agent job", "Executions", "Success", "Median", "P95", "Failures 24h"], [[i["job_name"], str(i["executions"]), "—" if i["success_percent"] is None else f"{i['success_percent']:.1f}%", "—" if i["median_seconds"] is None else f"{i['median_seconds']/60:.1f} min", "—" if i["p95_seconds"] is None else f"{i['p95_seconds']/60:.1f} min", str(i["failures_24h"])] for i in report["job_summary"]] or [["No Agent history", "-", "-", "-", "-", "-"]])
        _docx_table(document, ["Instance", "Breach", "Likely root cause", "Evidence"], [[i["instance"], i["breach"], i["likely_cause"], i["evidence"]] for i in report["performance_findings"]] or [["All instances", "No sustained performance breach", "No root-cause analysis required", "Selected period"]])
        _docx_table(document, ["SQL instance", "Samples total / CPU", "CPU median", "CPU P95", "CPU P99", "Sessions P95", "Blocking peak", "Grant peak"], [[i["instance_name"], f"{i['samples']} / {i['cpu_samples']}", "Not available" if not i["cpu_samples"] else f"{float(i['cpu']['median']):.1f}%", "Not available" if not i["cpu_samples"] else f"{float(i['cpu']['p95']):.1f}%", "Not available" if not i["cpu_samples"] else f"{float(i['cpu']['p99']):.1f}%", f"{float(i['sessions']['p95'] or 0):.1f}", f"{i['blocking_peak_seconds']}s", str(i["grants_peak"])] for i in report["performance_summary"]] or [["No performance history", "-", "-", "-", "-", "-", "-", "-"]])
        document.add_paragraph("Active request evidence is captured only when a request exceeds the configured duration. Query content is normalized with literal values redacted; source identifies stored procedures, triggers, ad hoc batches, database objects, or system work when SQL Server exposes that metadata.")
        _docx_table(document, ["Evidence", "Source", "Object / database", "Normalized query content", "Duration", "CPU", "Reads", "Evidence ID"], [[f"Query {rank}", i.get("source_type") or "Active request", i.get("source_name") or i["database_name"] or "Database not exposed", (i.get("query_text") or "Available after collector upgrade")[:240], f"{i['duration_ms']/1000:,.1f}s", f"{i['cpu_ms']/1000:,.1f}s", f"{i['logical_reads']:,}", str(i["query_signature"])[-12:]] for rank, i in enumerate(report["top_queries"][:10], 1)] or [["No active-query evidence", "-", "-", "-", "-", "-", "-", "-"]])
        document.add_heading("4 Incidents and warnings", level=1)
        _docx_table(document, ["Time", "Severity", "Target", "Description"], [[e["display_created_at"], e["severity"].title(), e["target"], e["message"]] for e in report["incidents"][:100]] or [["-", "None", "-", "No incidents or warnings recorded"]])
        document.add_heading("5 Services stability", level=1)
        _docx_table(document, ["Windows service", "Availability", "Incident events"], [[i["name"], f"{i['availability']:.3f}%", str(i["incidents"])] for i in report["services"]] or [["No monitored services", "-", "-"]])
        document.add_heading("6 Pools stability", level=1)
        _docx_table(document, ["IIS application pool", "Availability", "Incident events"], [[i["name"], f"{i['availability']:.3f}%", str(i["incidents"])] for i in report["pools"]] or [["No monitored pools", "-", "-"]])
        document.add_heading("7 Business impact for the client", level=1)
        for item in report["business_impact"]: document.add_paragraph(item, style="List Bullet")
        document.add_heading("8 Next actions", level=1)
        for item in report["next_actions"]: document.add_paragraph(item, style="List Number")
        footer = section.footer.paragraphs[0]; footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        footer.add_run("Millensys | MiMonitoringService | Confidential client report")
        destination.parent.mkdir(parents=True, exist_ok=True); document.save(destination)
    return destination


def export_pdf(store: Store, period: ReportPeriod, destination: Path) -> Path:
    report = build_analytics(store, period)
    with tempfile.TemporaryDirectory(prefix="mi-report-") as folder:
        charts = create_charts(report, Path(folder)); styles = getSampleStyleSheet()
        title = ParagraphStyle("ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=24, leading=29, textColor=colors.HexColor(NAVY), alignment=TA_CENTER, spaceAfter=10)
        heading = ParagraphStyle("ReportHeading", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=colors.HexColor(NAVY), spaceBefore=12, spaceAfter=8, keepWithNext=True)
        body = ParagraphStyle("ReportBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=9, leading=13, textColor=colors.HexColor("#27364b"))
        doc = SimpleDocTemplate(str(destination), pagesize=landscape(A4), leftMargin=10*mm, rightMargin=10*mm, topMargin=9*mm, bottomMargin=9*mm, title="Client Infrastructure Analytics Report")
        story: list[Any] = [Image(str(charts["dashboard"]), width=267*mm, height=150*mm), PageBreak(), Paragraph("MILLENSYS", ParagraphStyle("Brand", parent=body, textColor=colors.HexColor(TEAL), alignment=TA_CENTER, fontSize=10, leading=12)), Paragraph("Client Infrastructure Analytics Report", title)]
        client = report["client"].get("name") or "Client"
        story += [Paragraph(f"<b>{client}</b> | {report['server']}<br/>Reporting period {period.label}<br/>Prepared by Millensys Support Department", ParagraphStyle("Center", parent=body, alignment=TA_CENTER)), Spacer(1, 8*mm), Paragraph(f"This report summarizes infrastructure health, database capacity, backup assurance, incidents, and platform stability. Overall health averaged <b>{report['average_health']:.1f}/100</b> and monitored availability averaged <b>{report['availability']:.2f}%</b>.", body)]
        def add_heading(text: str) -> None: story.append(Paragraph(text, heading))
        header_text = ParagraphStyle(
            "TableHeader", parent=body, textColor=colors.white, fontName="Helvetica-Bold"
        )
        def add_table(headers: list[str], rows: list[list[str]], widths=None) -> None:
            if widths:
                scale = 277 * mm / sum(widths)
                widths = [width * scale for width in widths]
            data = [[Paragraph(h, header_text) for h in headers]] + [[Paragraph(str(v), body) for v in row] for row in rows]
            table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([("BACKGROUND", (0,0),(-1,0),colors.HexColor(NAVY)),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),.35,colors.HexColor("#d9d9d9")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
            for row in range(2, len(data), 2): table.setStyle(TableStyle([("BACKGROUND",(0,row),(-1,row),colors.HexColor("#eef4f8"))]))
            story.extend([table, Spacer(1, 4*mm)])
        add_heading("1 Duration overview summary")
        add_table(["Measure", "Result"], [["Reporting period", period.label],["Server health average",f"{report['average_health']:.1f}/100"],["Incidents and warnings",str(len(report['incidents']))],["Recoveries",str(len(report['recoveries']))],["Backup validation pass rate",f"{report['backup_rate']:.1f}%"],["SQL Agent success percentage",f"{report['agent_success_percent']:.1f}%"],["Service and pool availability",f"{report['availability']:.2f}%"]], [105*mm, 65*mm])
        add_heading("2 Detailed operational evidence")
        story.append(Paragraph("The dashboard summarizes health, utilization, capacity growth, backup compliance, threshold breaches, and operational risk. The following tables retain the supporting evidence for review and audit.", body))
        add_heading("3 Database health and backup validation")
        add_table(["Database","Start MB","End MB","Growth MB","ONLINE"], [[i["name"],f"{i['start_mb']:.1f}",f"{i['end_mb']:.1f}",f"{i['growth_mb']:+.1f}",f"{i['healthy_percent']:.1f}%"] for i in report["databases"]] or [["No database samples","-","-","-","-"]], [64*mm,25*mm,25*mm,28*mm,28*mm])
        add_table(["Date","Backup validation"], [[i["date"],"Passed" if i["ok"] else "Failed" if i["checked"] else "No recorded check"] for i in report["backup_days"]], [55*mm,115*mm])
        add_table(["SQL-native evidence","Count"], [["Full/differential/log backup sets",str(len(report["sql_backups"]))],["Verification and restore-test records",str(len(report["recovery_evidence"]))]], [125*mm,45*mm])
        add_table(["Date","Database","Recovery evidence","Result","Owner"], [[i["performed_at"][:10],i["database_name"],i["evidence_type"],i["result"],i["owner"]] for i in report["recovery_evidence"]] or [["-","-","No evidence in period","-","-"]], [25*mm,30*mm,55*mm,25*mm,35*mm])
        add_table(["SQL Agent job","Runs","Success","Median","P95","24h failures"], [[i["job_name"],str(i["executions"]),"—" if i["success_percent"] is None else f"{i['success_percent']:.1f}%","—" if i["median_seconds"] is None else f"{i['median_seconds']/60:.1f}m","—" if i["p95_seconds"] is None else f"{i['p95_seconds']/60:.1f}m",str(i["failures_24h"])] for i in report["job_summary"]] or [["No Agent history","-","-","-","-","-"]], [55*mm,18*mm,25*mm,24*mm,22*mm,26*mm])
        add_table(["Instance","Breach","Likely root cause","Evidence"], [[i["instance"],i["breach"],i["likely_cause"],i["evidence"]] for i in report["performance_findings"]] or [["All instances","No sustained performance breach","No root-cause analysis required","Selected period"]], [42*mm,50*mm,125*mm,60*mm])
        add_table(["SQL instance","Samples total / CPU","CPU median","CPU P95","CPU P99","Sessions P95","Blocking","Grants"], [[i["instance_name"],f"{i['samples']} / {i['cpu_samples']}","Not available" if not i["cpu_samples"] else f"{float(i['cpu']['median']):.1f}%","Not available" if not i["cpu_samples"] else f"{float(i['cpu']['p95']):.1f}%","Not available" if not i["cpu_samples"] else f"{float(i['cpu']['p99']):.1f}%",f"{float(i['sessions']['p95'] or 0):.1f}",f"{i['blocking_peak_seconds']}s",str(i["grants_peak"])] for i in report["performance_summary"]] or [["No performance history","-","-","-","-","-","-","-"]], [55*mm,32*mm,36*mm,32*mm,32*mm,36*mm,27*mm,27*mm])
        story.append(Paragraph("Active request evidence is captured only when a request exceeds the configured duration. Query content is normalized with literal values redacted; source identifies stored procedures, triggers, ad hoc batches, database objects, or system work when SQL Server exposes that metadata.", body))
        story.append(Spacer(1, 2*mm))
        add_table(["Evidence","Source","Object / database","Normalized query content","Duration","CPU","Reads","Evidence ID"], [[f"Query {rank}",i.get("source_type") or "Active request",i.get("source_name") or i["database_name"] or "Database not exposed",(i.get("query_text") or "Available after collector upgrade")[:240],f"{i['duration_ms']/1000:,.1f}s",f"{i['cpu_ms']/1000:,.1f}s",f"{i['logical_reads']:,}",str(i["query_signature"])[-12:]] for rank,i in enumerate(report["top_queries"][:10],1)] or [["No active-query evidence","-","-","-","-","-","-","-"]], [22*mm,34*mm,48*mm,80*mm,25*mm,22*mm,24*mm,42*mm])
        add_heading("4 Incidents and warnings")
        add_table(["Time","Severity","Target","Description"], [[e["display_created_at"],e["severity"].title(),e["target"],e["message"]] for e in report["incidents"][:100]] or [["-","None","-","No incidents or warnings recorded"]], [34*mm,22*mm,38*mm,76*mm])
        add_heading("5 Services stability")
        add_table(["Windows service","Availability","Incident events"], [[i["name"],f"{i['availability']:.3f}%",str(i["incidents"])] for i in report["services"]] or [["No monitored services","-","-"]], [100*mm,35*mm,35*mm])
        add_heading("6 Pools stability")
        add_table(["IIS application pool","Availability","Incident events"], [[i["name"],f"{i['availability']:.3f}%",str(i["incidents"])] for i in report["pools"]] or [["No monitored pools","-","-"]], [100*mm,35*mm,35*mm])
        add_heading("7 Business impact for the client")
        for item in report["business_impact"]: story.append(Paragraph(f"- {item}", body))
        add_heading("8 Next actions")
        for index, item in enumerate(report["next_actions"], 1): story.append(Paragraph(f"{index}. {item}", body))
        def footer(canvas, document):
            canvas.saveState(); canvas.setFont("Helvetica", 8); canvas.setFillColor(colors.HexColor("#64748b"))
            canvas.drawString(16*mm, 9*mm, "Millensys | MiMonitoringService | Confidential client report")
            canvas.drawRightString(194*mm, 9*mm, f"Page {document.page}"); canvas.restoreState()
        destination.parent.mkdir(parents=True, exist_ok=True); doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return destination
