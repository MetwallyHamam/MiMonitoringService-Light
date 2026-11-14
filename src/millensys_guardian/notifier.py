from __future__ import annotations

import hashlib
import json
import mimetypes
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from pathlib import Path

from .security import unprotect
from .store import Store, utc_now
from .system_info import server_identity
from .time_utils import server_time_text


def branded_html(title: str, body: str, severity: str = "info") -> str:
    colors = {"critical": "#dc2626", "warning": "#f59e0b", "recovery": "#16a34a", "info": "#2563eb"}
    color = colors.get(severity, colors["info"])
    return f"""<!doctype html><html><body style="margin:0;background:#f3f6fb;font-family:Segoe UI,Arial,sans-serif;color:#172033">
    <table width="100%" role="presentation"><tr><td align="center" style="padding:32px 12px">
    <table width="640" role="presentation" style="max-width:640px;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 8px 30px #15213a1f">
    <tr><td style="background:#101c35;padding:24px 30px;color:#fff"><div style="font-size:12px;letter-spacing:2px;color:#8ab4ff">MILLENSYS</div><div style="font-size:22px;font-weight:700;margin-top:6px">MiMonitoringService</div></td></tr>
    <tr><td style="height:5px;background:{color}"></td></tr><tr><td style="padding:30px"><h2 style="margin:0 0 16px">{escape(title)}</h2>{body}</td></tr>
    <tr><td style="padding:18px 30px;background:#f7f9fc;color:#64748b;font-size:12px">Automated infrastructure notification · __SERVER_TIME__</td></tr>
    </table></td></tr></table></body></html>"""


class Notifier:
    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def fingerprint(category: str, target: str, condition: str) -> str:
        return hashlib.sha256(f"{category}|{target}|{condition}".encode()).hexdigest()

    def alert(
        self,
        category: str,
        target: str,
        condition: str,
        message: str,
        severity: str,
        *,
        repeat: bool = True,
    ) -> None:
        fingerprint = self.fingerprint(category, target, condition)
        config = self.store.get("configuration")["email"]
        now = datetime.now(UTC)
        should_send = False
        new_cycle = False
        with self.store.connect() as db:
            state = db.execute(
                "SELECT * FROM alert_state WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if state is None or not state["active"]:
                should_send = True
                new_cycle = True
            elif repeat and state["last_sent"]:
                last = datetime.fromisoformat(state["last_sent"])
                should_send = now - last >= timedelta(
                    minutes=int(config.get("cooldown_minutes", 30))
                )
            db.execute(
                "INSERT INTO alert_state(fingerprint,active,last_sent,last_seen,occurrences,category,target,condition,message,severity,first_seen) "
                "VALUES(?,?,?,?,1,?,?,?,?,?,?) "
                "ON CONFLICT(fingerprint) DO UPDATE SET "
                "first_seen=CASE WHEN alert_state.active=0 THEN excluded.first_seen ELSE alert_state.first_seen END,"
                "active=1,last_seen=excluded.last_seen,category=excluded.category,target=excluded.target,"
                "condition=excluded.condition,message=excluded.message,severity=excluded.severity,"
                "occurrences=alert_state.occurrences+1,"
                "last_sent=CASE WHEN ? THEN excluded.last_sent ELSE alert_state.last_sent END",
                (
                    fingerprint,
                    1,
                    now.isoformat(),
                    now.isoformat(),
                    category,
                    target,
                    condition,
                    message,
                    severity,
                    now.isoformat(),
                    should_send,
                ),
            )
        if new_cycle:
            self.store.record_incident_cycle(fingerprint, category, target, condition)
        if should_send:
            body = f"<p style='font-size:15px;line-height:1.6'>{escape(message)}</p><p><b>Item:</b> {escape(target)}</p>"
            self.enqueue(
                f"[{severity.upper()}] {target} · {condition}",
                branded_html(condition, body, severity),
            )

    def recover(self, category: str, target: str, condition: str, message: str) -> None:
        fingerprint = self.fingerprint(category, target, condition)
        self.store.resolve_score_incident_by_key(
            f"active:{fingerprint}", "Target recovered automatically"
        )
        recovered = False
        with self.store.connect() as db:
            state = db.execute(
                "SELECT active FROM alert_state WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if state and state["active"]:
                db.execute(
                    "UPDATE alert_state SET active=0,last_seen=? WHERE fingerprint=?",
                    (utc_now(), fingerprint),
                )
                recovered = True
        # Queue mail only after the alert-state transaction commits. Opening a second
        # writer while the update is active causes SQLite to report "database is locked".
        if recovered:
            body = f"<p style='font-size:15px;line-height:1.6'>{escape(message)}</p>"
            self.enqueue(
                f"[RECOVERED] {target}", branded_html("Service restored", body, "recovery")
            )

    def suppress(self, category: str, target: str, condition: str) -> None:
        """Close a stale alert without sending a recovery notification."""
        fingerprint = self.fingerprint(category, target, condition)
        with self.store.connect() as db:
            db.execute(
                "UPDATE alert_state SET active=0,last_seen=? WHERE fingerprint=?",
                (utc_now(), fingerprint),
            )

    def enqueue(
        self,
        subject: str,
        html: str,
        recipients: list[str] | None = None,
        attachments: list[str] | None = None,
    ) -> None:
        config = self.store.get("configuration")
        recipients = recipients or config["email"].get("recipients", [])
        if not recipients:
            return
        subject = self._subject(subject, config)
        html = self._with_identity(html, config)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO mail_queue(created_at,next_attempt,subject,html,recipients,attachments) "
                "VALUES(?,?,?,?,?,?)",
                (utc_now(), utc_now(), subject, html, json.dumps(recipients), json.dumps(attachments or [])),
            )

    def flush(self, limit: int = 10) -> None:
        config = self.store.get("configuration")["email"]
        if not config.get("enabled") or not config.get("password_protected"):
            return
        with self.store.connect() as db:
            rows = list(
                db.execute(
                    "SELECT * FROM mail_queue WHERE status='queued' AND next_attempt<=? ORDER BY id LIMIT ?",
                    (utc_now(), limit),
                )
            )
        for row in rows:
            try:
                self._deliver(
                    config,
                    row["subject"],
                    row["html"],
                    json.loads(row["recipients"]),
                    json.loads(row["attachments"]),
                )
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE mail_queue SET status='sent',attempts=attempts+1 WHERE id=?",
                        (row["id"],),
                    )
            # A queued notification must survive provider, TLS, credential, and DPAPI failures.
            except Exception as exc:  # noqa: BLE001
                attempts = int(row["attempts"]) + 1
                max_attempts = max(1, int(config.get("max_attempts", 5)))
                delay = min(3600, 60 * (2 ** int(row["attempts"])))
                retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="seconds")
                status = "failed" if attempts >= max_attempts else "queued"
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE mail_queue SET attempts=?,status=?,next_attempt=?,last_error=? WHERE id=?",
                        (attempts, status, retry, str(exc)[:500], row["id"]),
                    )
                if status == "failed":
                    self.store.add_event(
                        "error",
                        "email",
                        str(row["id"]),
                        "Email quarantined after maximum delivery attempts",
                        error=str(exc)[:500],
                    )

    def send_test(self) -> tuple[bool, str]:
        config = self.store.get("configuration")["email"]
        recipients = config.get("recipients", [])
        if not recipients:
            return False, "Add at least one recipient before testing SMTP."
        if not config.get("password_protected"):
            return False, "Save the SMTP app password before testing."
        html = branded_html(
            "SMTP test successful",
            "<p>MiMonitoringService can deliver notifications from this server.</p>",
            "recovery",
        )
        try:
            self._deliver(
                config,
                self._subject("[TEST] MiMonitoringService", self.store.get("configuration")),
                self._with_identity(html, self.store.get("configuration")),
                recipients,
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, f"Test email sent to {', '.join(recipients)}."

    @staticmethod
    def _deliver(
        config: dict,
        subject: str,
        html: str,
        recipients: list[str],
        attachments: list[str] | None = None,
    ) -> None:
        message = EmailMessage()
        message["From"] = formataddr(
            (config.get("sender_name", "MiMonitoringService"), config["username"])
        )
        message["To"] = ", ".join(recipients)
        if config.get("cc"):
            message["Cc"] = ", ".join(config["cc"])
        message["Subject"] = subject
        message.set_content("This notification contains an HTML report.")
        message.add_alternative(html, subtype="html")
        for filename in attachments or []:
            path = Path(filename)
            if not path.is_file():
                raise FileNotFoundError(f"Report attachment is unavailable: {path}")
            content_type, _ = mimetypes.guess_type(path.name)
            major, minor = (content_type or "application/octet-stream").split("/", 1)
            message.add_attachment(path.read_bytes(), maintype=major, subtype=minor, filename=path.name)
        with smtplib.SMTP(config["smtp_server"], int(config["smtp_port"]), timeout=25) as smtp:
            if config.get("use_tls", True):
                smtp.starttls()
            smtp.login(config["username"], unprotect(config["password_protected"]))
            smtp.send_message(message)

    @staticmethod
    def _subject(title: str, config: dict) -> str:
        client = config.get("client", {}).get("name", "").strip() or "Millensys"
        prefix = f"{client} | "
        return title if title.startswith(prefix) else f"{prefix}{title}"

    @staticmethod
    def _with_identity(html: str, config: dict) -> str:
        identity = server_identity()
        client = config.get("client", {})
        block = (
            "<div style='margin:20px 0;padding:14px;background:#f1f5f9;border-radius:8px;"
            "font-size:13px;color:#334155'>"
            f"<b>Client:</b> {escape(str(client.get('name') or 'Not configured'))}<br>"
            f"<b>Server:</b> {escape(identity.server_name)}<br>"
            f"<b>MAC address:</b> {escape(identity.mac_address)}"
            "</div>"
        )
        html = html.replace("__SERVER_TIME__", escape(server_time_text(utc_now(), config)))
        marker = "<tr><td style=\"padding:18px 30px;background:#f7f9fc"
        return html.replace(marker, f"<tr><td style='padding:0 30px'>{block}</td></tr>{marker}", 1)
