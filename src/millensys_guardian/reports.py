from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from html import escape

from .analytics_report import ReportPeriod, export_pdf
from .notifier import Notifier, branded_html
from .scheduler import configured_timezone, latest_occurrence
from .store import Store


def queue_period_report(store: Store, period: str = "daily", now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    current_utc = current.astimezone(UTC)
    if period == "daily":
        since = (current_utc - timedelta(days=1)).isoformat()
    else:
        first_this_month = current_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_month_end = first_this_month - timedelta(microseconds=1)
        since = previous_month_end.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
    with store.connect() as db:
        rows = list(
            db.execute("SELECT * FROM events WHERE created_at>=? ORDER BY id DESC", (since,))
        )
    severities = Counter(row["severity"] for row in rows)
    notable = [
        row for row in rows if row["severity"] in {"critical", "error", "warning", "recovery"}
    ][:30]
    list_html = (
        "".join(
            f"<tr><td style='padding:8px'>{escape(row['created_at'][:19])}</td><td>{escape(row['severity'])}</td><td>{escape(row['target'])}</td><td>{escape(row['message'])}</td></tr>"
            for row in notable
        )
        or "<tr><td colspan='4' style='padding:16px'>No notable incidents in this period.</td></tr>"
    )
    config = store.get("configuration")
    report_config = config.get("reports", {})
    client = config.get("client", {})
    sections = []
    if report_config.get("include_summary", True):
        sections.append(
            f"<p><b>{len(rows)}</b> events · <b>{severities['critical'] + severities['error']}</b> critical/errors · <b>{severities['recovery']}</b> recoveries</p>"
        )
    if report_config.get("include_incidents", True):
        sections.append(f"<table width='100%' style='border-collapse:collapse'>{list_html}</table>")
    identity = " · ".join(
        escape(str(value))
        for value in (client.get("name"), client.get("site"), client.get("server_label"))
        if value
    )
    body = (f"<p style='color:#64748b'>{identity}</p>" if identity else "") + "".join(sections)
    recipients = report_config.get("recipients") or config["email"].get("recipients", [])
    attachments: list[str] = []
    if period == "monthly" and report_config.get("monthly_analytics_attachment", True):
        period_start = datetime.fromisoformat(since)
        report_period = ReportPeriod(period_start, previous_month_end)
        client_name = str(client.get("name") or "Client").strip().replace(" ", "-")
        filename = store.path.parent / "reports" / f"{client_name}-analytics-{period_start:%Y-%m}.pdf"
        export_pdf(store, report_period, filename)
        attachments.append(str(filename))
    Notifier(store).enqueue(
        f"Millensys {period.title()} Server Health Report",
        branded_html(f"{period.title()} server health report", body),
        recipients,
        attachments,
    )


class ReportScheduler:
    def __init__(self, store: Store) -> None:
        self.store = store

    def process(self, now: datetime | None = None) -> None:
        config = self.store.get("configuration")
        reports = config.get("reports", {})
        timezone = configured_timezone(config["agent"].get("timezone", "UTC"))
        current = (now or datetime.now(UTC)).astimezone(timezone)
        definitions = (
            (
                "daily",
                {
                    "enabled": reports.get("daily_enabled", True),
                    "frequency": "Daily",
                    "time": reports.get("daily_time", "08:00"),
                },
            ),
            (
                "monthly",
                {
                    "enabled": reports.get("monthly_enabled", True),
                    "frequency": "Monthly",
                    "month_day": reports.get("monthly_day", 1),
                    "time": reports.get("monthly_time", "08:15"),
                },
            ),
        )
        for period, schedule in definitions:
            occurrence = latest_occurrence(schedule, current)
            if occurrence is None or current - occurrence > timedelta(minutes=30):
                continue
            stamp = occurrence.astimezone(UTC).isoformat(timespec="minutes")
            claim_key = f"report:{period}:{stamp}"
            if not self.store.claim_action(
                claim_key,
                "report",
                period,
                "queue",
                stamp,
                retry_failed=True,
                max_attempts=3,
                retry_delay_seconds=60,
            ):
                continue
            try:
                queue_period_report(self.store, period, current)
            # Report generation must not terminate the long-running monitoring service.
            except Exception as exc:  # noqa: BLE001
                self.store.finish_action(claim_key, False, str(exc))
                self.store.add_event(
                    "error", "report", period, f"{period.title()} report generation failed"
                )
            else:
                self.store.finish_action(claim_key, True, "queued")
                self.store.add_event("info", "report", period, f"{period.title()} report queued")
