from __future__ import annotations

import csv
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from itertools import pairwise
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6.QtCore import QDate, QSize, Qt, QTime, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .actions import pool_action, service_action
from .analytics import ATTENTION_MINIMUM, HEALTHY_MINIMUM, assess_health
from .analytics_report import ReportPeriod, build_analytics, export_docx, export_pdf, this_month
from .assurance import AssuranceMonitor, friendly_property_name
from .discovery import disks, iis_pools, performance, performance_consumers, windows_services
from .licensing import create_activation_request, current_license, import_license
from .notifier import Notifier, branded_html
from .registration import validate_operator, validate_recipients
from .resources import resource_path
from .security import protect
from .service_control import manage as manage_agent_service
from .service_control import status as agent_service_status
from .sql_backup import recovery_readiness
from .sql_capacity import capacity_forecasts, forecast_text
from .sql_monitor import kill_sessions, start_agent_job
from .sql_monitor import test_connection as test_sql_connection
from .store import Store
from .system_info import server_identity
from .time_utils import server_time_text, server_timezone

COPYRIGHT = "© 2026 Millensys Support Department | Eng.Metwally Hammam"


def display_server_time(store: Store, value: str | datetime | None, pattern: str = "%Y-%m-%d %H:%M:%S") -> str:
    return server_time_text(value, store.get("configuration"), pattern)


def open_user_guide(parent: QWidget | None = None) -> None:
    guide = resource_path("docs/user-guide.html")
    if not guide.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide))):
        QMessageBox.warning(parent, "Guide unavailable", f"Cannot open the offline guide:\n{guide}")


class ActivationDialog(QDialog):
    def __init__(self, store: Store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Activate MiMonitoringService")
        self.setMinimumSize(660, 430)
        config = store.get("configuration")
        layout = QVBoxLayout(self)
        title = QLabel("Millensys license activation")
        title.setFont(QFont("Segoe UI", 23, QFont.Weight.Bold))
        layout.addWidget(title)
        description = QLabel(
            "This server requires a signed Millensys license. For an offline server, create "
            "a request file, transfer it to Millensys Support, then import the issued .milic file."
        )
        description.setWordWrap(True)
        description.setStyleSheet("color:#98a6aa")
        layout.addWidget(description)
        self.offline_server = QCheckBox("This monitored server is offline")
        self.offline_server.setChecked(True)
        self.offline_server.setToolTip(
            "Export a request to an online PC running the private Millensys License Generator."
        )
        layout.addWidget(self.offline_server)
        form = QFormLayout()
        self.client_name = QLineEdit(config["client"].get("name", ""))
        self.client_name.setPlaceholderText("Licensed client or company")
        self.operator_email = QLineEdit(config["client"].get("operator_email", ""))
        self.operator_email.setPlaceholderText("engineer@millensys.com")
        form.addRow("Client name", self.client_name)
        form.addRow("Millensys operator", self.operator_email)
        layout.addLayout(form)
        self.status = QLabel(current_license().message)
        self.status.setWordWrap(True)
        self.status.setObjectName("statusBad")
        layout.addWidget(self.status)
        layout.addStretch()
        buttons = QHBoxLayout()
        request_button = QPushButton("Create offline request…")
        request_button.clicked.connect(self.create_request)
        import_button = QPushButton("Import signed license…")
        import_button.setObjectName("primary")
        import_button.clicked.connect(self.import_signed_license)
        buttons.addWidget(request_button)
        buttons.addStretch()
        buttons.addWidget(import_button)
        layout.addLayout(buttons)

    def _validate_identity(self) -> bool:
        if not self.client_name.text().strip():
            QMessageBox.warning(self, "Client required", "Enter the licensed client name.")
            return False
        result = validate_operator(
            self.operator_email.text(), self.store.get("configuration")["registration"]
        )
        if not result.valid:
            QMessageBox.warning(self, "Millensys email required", result.message)
            return False
        return True

    def create_request(self) -> None:
        if not self._validate_identity():
            return
        if not self.offline_server.isChecked():
            QMessageBox.information(
                self,
                "Select activation mode",
                "Online self-activation requires the future Millensys licensing API. "
                "For the current release, mark this server as offline and export a request.",
            )
            return
        default_name = f"MiMonitoringService-{server_identity().server_name}.mirequest"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save activation request", default_name, "Activation request (*.mirequest)"
        )
        if not filename:
            return
        try:
            create_activation_request(
                Path(filename),
                self.operator_email.text(),
                self.client_name.text(),
                activation_mode="offline",
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Cannot create request", str(exc))
            return
        QMessageBox.information(
            self,
            "Request created",
            "Send the .mirequest file to Millensys Support. No SMTP password or private key is included.",
        )

    def import_signed_license(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Import Millensys license", "", "Millensys license (*.milic)"
        )
        if not filename:
            return
        try:
            status, smtp_password = import_license(Path(filename))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "License import failed", str(exc))
            return
        if not status.valid:
            QMessageBox.warning(self, "Invalid license", status.message)
            return
        config = self.store.get("configuration")
        config["client"]["name"] = str(status.payload.get("client_name", ""))
        config["client"]["operator_email"] = str(status.payload.get("operator_email", ""))
        smtp_config = status.payload.get("smtp_config", {})
        for key in ("smtp_server", "smtp_port", "username", "sender_name", "use_tls"):
            if key in smtp_config:
                config["email"][key] = smtp_config[key]
        config["email"]["password_protected"] = protect(smtp_password)
        self.store.set("configuration", config)
        self.store.add_event(
            "info", "license", "activation", f"License activated; {status.message}"
        )
        QMessageBox.information(self, "Activation successful", status.message)
        self.accept()

STYLE = """
QWidget { color:#e6ecee; font-family:'Segoe UI'; font-size:9pt; }
QMainWindow, QDialog { background:#101416; }
QFrame#sidebar { background:#161b1e; border-right:1px solid #30383c; }
QFrame#card { background:#1b2124; border:1px solid #30383c; border-radius:12px; }
QFrame#hero { background:#1d2528; border:1px solid #34717a; border-radius:12px; }
QFrame#metricCard { background:#1b2124; border:1px solid #354044; border-radius:12px; }
QFrame#metricCard QLabel { background:transparent; }
QFrame#metricCard QProgressBar { background:#111719; border-radius:4px; height:7px; }
QLabel#eyebrow { color:#45ced9; font-size:8pt; font-weight:700; }
QLabel#statusGood { color:#58d68d; font-weight:700; }
QLabel#statusWarn { color:#f2b84b; font-weight:700; }
QLabel#statusBad { color:#ff6577; font-weight:700; }
QPushButton { background:#22292d; border:1px solid #3a4449; border-radius:6px; padding:5px 10px; min-height:20px; text-align:left; }
QPushButton:hover { background:#2b3438; border-color:#536066; } QPushButton:pressed { background:#1b2124; }
QPushButton#primary { background:#20aebe; border:1px solid #35c7d4; color:#071416; font-weight:700; text-align:center; }
QPushButton#primary:hover { background:#35c7d4; }
QPushButton#danger { background:#51232a; border-color:#8e3947; color:#ffdce1; text-align:center; }
QPushButton#navButton { background:transparent; border:1px solid transparent; color:#aeb9bd; padding:7px 10px; }
QPushButton#navButton:hover { background:#20272a; color:#f3f7f8; border-color:#30383c; }
QPushButton#navButton:checked { background:#203236; color:#67dbe4; border-color:#34717a; border-left:3px solid #35c7d4; font-weight:600; }
QLineEdit, QSpinBox, QComboBox, QTextEdit { background:#151a1d; border:1px solid #3a4449; border-radius:6px; padding:5px 7px; min-height:20px; selection-background-color:#287d86; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus { border-color:#35c7d4; }
QTabWidget::pane { background:#171c1f; border:1px solid #30383c; border-radius:8px; top:-1px; }
QTabWidget QWidget { background:#171c1f; }
QTabBar { background:transparent; }
QTabBar::tab { background:#202629; color:#9da9ad; border:1px solid #30383c; border-bottom:0; padding:7px 14px; margin-right:3px; border-top-left-radius:7px; border-top-right-radius:7px; }
QTabBar::tab:hover { background:#293135; color:#edf3f4; }
QTabBar::tab:selected { background:#263236; color:#67dbe4; border-color:#34717a; border-top:2px solid #35c7d4; font-weight:600; }
QTabBar::tab:!selected { margin-top:3px; }
QTabWidget QLabel, QTabWidget QCheckBox { background:transparent; }
QTableWidget { background:#171c1f; alternate-background-color:#1c2225; border:1px solid #30383c; border-radius:8px; gridline-color:#2a3135; selection-background-color:#28545a; }
QListWidget { background:#171c1f; alternate-background-color:#1c2225; border:1px solid #30383c; border-radius:8px; outline:0; padding:3px; }
QListWidget::item { padding:7px 8px; border-bottom:1px solid #292f32; }
QListWidget::item:hover { background:#242c2f; }
QListWidget::item:selected { background:#28545a; color:#ffffff; }
QProgressBar { background:#111719; border:0; border-radius:4px; height:7px; text-align:center; color:transparent; }
QProgressBar::chunk { background:#35c7d4; border-radius:4px; }
QHeaderView::section { background:#22292d; color:#adb9bd; border:0; border-bottom:1px solid #3a4449; padding:6px; font-weight:600; }
QMenu { background:#1b2124; border:1px solid #3a4449; padding:4px; }
QMenu::item:selected { background:#28545a; }
QToolTip { background:#f3f6f6; color:#162023; border:1px solid #aeb9bd; }
"""


class TrendWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.samples: list[tuple[float, float, float | None]] = []
        self.setMinimumHeight(150)
        self.setToolTip("CPU is blue; memory is green. Values cover the configured trend period.")

    def set_samples(self, rows: list) -> None:
        bucket_size = max(1, (len(rows) + 119) // 120)
        self.samples = []
        for offset in range(0, len(rows), bucket_size):
            bucket = rows[offset:offset + bucket_size]
            disk = [float(row["minimum_disk_free_percent"]) for row in bucket
                    if row["minimum_disk_free_percent"] is not None]
            self.samples.append((
                median(float(row["cpu"]) for row in bucket),
                median(float(row["memory"]) for row in bucket),
                median(disk) if disk else None,
            ))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#171c1f"))
        margin, width, height = 18, max(1, self.width() - 36), max(1, self.height() - 34)
        painter.setPen(QPen(QColor("#354044"), 1))
        for percent in (0, 25, 50, 75, 100):
            y = margin + height - (percent / 100 * height)
            painter.drawLine(margin, int(y), margin + width, int(y))
        if len(self.samples) < 2:
            painter.setPen(QColor("#98a6aa"))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "Trend data appears after the agent runs"
            )
            return
        for value_index, color in ((0, "#35c7d4"), (1, "#58d68d"), (2, "#f2b84b")):
            pen = QPen(QColor(color), 1.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            points = []
            for index, sample in enumerate(self.samples):
                x = margin + index * width / (len(self.samples) - 1)
                value = sample[value_index]
                if value is None:
                    continue
                y = margin + height - min(max(value, 0), 100) / 100 * height
                points.append((int(x), int(y)))
            for first, second in pairwise(points):
                painter.drawLine(*first, *second)
        painter.setPen(QColor("#98a6aa"))
        painter.drawText(margin, self.height() - 5, "CPU")
        painter.setPen(QColor("#58d68d"))
        painter.drawText(margin + 42, self.height() - 5, "Memory")
        painter.setPen(QColor("#f2b84b"))
        painter.drawText(margin + 105, self.height() - 5, "Disk free")


class SqlPerformanceTrendWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.points: list[tuple[float, float]] = []
        self.setMinimumHeight(135)
        self.setToolTip("Teal shows SQL process CPU percentage; amber shows active user sessions relative to the observed peak.")

    def set_samples(self, rows: list) -> None:
        peak_sessions = max((int(row["active_sessions"]) for row in rows), default=1)
        self.points = [(float(row["sql_cpu_percent"] or 0),
                        100.0 * int(row["active_sessions"]) / max(1, peak_sessions)) for row in rows]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#171c1f"))
        margin, width, height = 18, max(1, self.width() - 36), max(1, self.height() - 32)
        painter.setPen(QPen(QColor("#354044"), 1))
        for percent in (0, 25, 50, 75, 100):
            y = margin + height - percent / 100 * height; painter.drawLine(margin, int(y), margin + width, int(y))
        if len(self.points) < 2:
            painter.setPen(QColor("#98a6aa")); painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "SQL trend appears after multiple performance samples"); return
        for value_index, color in ((0, "#35c7d4"), (1, "#f2b84b")):
            points = [(int(margin + index * width / (len(self.points) - 1)),
                       int(margin + height - min(100, max(0, point[value_index])) / 100 * height))
                      for index, point in enumerate(self.points)]
            painter.setPen(QPen(QColor(color), 1.0))
            for first, second in pairwise(points): painter.drawLine(*first, *second)
        painter.setPen(QColor("#35c7d4")); painter.drawText(margin, self.height() - 4, "SQL CPU")
        painter.setPen(QColor("#f2b84b")); painter.drawText(margin + 65, self.height() - 4, "Active sessions index")


class CapacityTrendWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.points: list[tuple[float, float]] = []
        self.setMinimumHeight(120)
        self.setMaximumHeight(165)
        self.setToolTip(
            "Teal shows allocated database size relative to the first day. Amber shows hosting-volume free space relative to the first day."
        )

    def set_samples(self, rows: list) -> None:
        by_day: dict[str, dict[str, float]] = {}
        for row in rows:
            day = str(row["day"])
            values = by_day.setdefault(day, {"allocated": 0.0, "volume_free": 0.0})
            values["allocated"] += float(row["end_allocated_mb"])
            if row["min_volume_free_mb"] is not None:
                values["volume_free"] += float(row["min_volume_free_mb"])
        ordered = [by_day[day] for day in sorted(by_day)]
        if not ordered:
            self.points = []
        else:
            first_allocated = max(1.0, ordered[0]["allocated"])
            first_free = max(1.0, ordered[0]["volume_free"])
            self.points = [
                (
                    100.0 * item["allocated"] / first_allocated,
                    100.0 * item["volume_free"] / first_free,
                )
                for item in ordered
            ]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#171c1f"))
        margin, width, height = 20, max(1, self.width() - 40), max(1, self.height() - 36)
        painter.setPen(QPen(QColor("#354044"), 1))
        for fraction in (0.0, 0.5, 1.0):
            y = margin + height * fraction
            painter.drawLine(margin, int(y), margin + width, int(y))
        if len(self.points) < 2:
            painter.setPen(QColor("#98a6aa"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Capacity trend appears after samples from multiple days",
            )
            return
        values = [value for point in self.points for value in point]
        low, high = min(values), max(values)
        padding = max(1.0, (high - low) * 0.15)
        low -= padding
        high += padding
        for value_index, color in ((0, "#35c7d4"), (1, "#f2b84b")):
            points = []
            for index, sample in enumerate(self.points):
                x = margin + index * width / (len(self.points) - 1)
                y = margin + height - (sample[value_index] - low) / (high - low) * height
                points.append((int(x), int(y)))
            painter.setPen(QPen(QColor(color), 2))
            for first, second in pairwise(points):
                painter.drawLine(*first, *second)
        painter.setPen(QColor("#35c7d4"))
        painter.drawText(margin, self.height() - 5, "Allocated size index")
        painter.setPen(QColor("#f2b84b"))
        painter.drawText(margin + 130, self.height() - 5, "Volume free index")


class BackupCalendarWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.days: list[dict] = []
        self.setMinimumHeight(78)
        self.setMaximumHeight(92)

    def set_days(self, days: list[dict]) -> None:
        self.days = days[-31:]
        counts = {status: sum(1 for day in self.days if day.get("status") == status) for status in ("Compliant", "Attention", "No evidence")}
        self.setToolTip(f"31-day evidence: {counts['Compliant']} compliant · {counts['Attention']} attention · {counts['No evidence']} no evidence")
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#171c1f"))
        if not self.days:
            painter.setPen(QColor("#98a6aa")); painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "31-day backup evidence appears after SQL history collection")
            return
        margin, gap = 12, 3
        width = max(8, (self.width() - margin * 2 - gap * 30) / 31)
        colors = {"Compliant": "#58d68d", "Attention": "#f2b84b", "No evidence": "#ff6577"}
        for index, day in enumerate(self.days):
            x = margin + index * (width + gap)
            painter.setBrush(QColor(colors.get(str(day.get("status")), "#536066"))); painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(int(x), 16, int(width), 28, 3, 3)
        painter.setPen(QColor("#98a6aa")); painter.drawText(margin, 65, str(self.days[0].get("day", ""))[5:])
        painter.drawText(max(margin, self.width() - 50), 65, str(self.days[-1].get("day", ""))[5:])


class BackupChainTimelineWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent); self.history: list[dict] = []; self.setFixedHeight(64)

    def set_history(self, rows: list[dict]) -> None:
        self.history = sorted(rows, key=lambda item: str(item.get("finish_time", "")))[-80:]; self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self); painter.fillRect(self.rect(), QColor("#171c1f"))
        if not self.history:
            painter.setPen(QColor("#98a6aa")); painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Backup-chain timeline appears after SQL-native history collection"); return
        margin, width = 14, max(1, self.width() - 28); y = 27
        painter.setPen(QPen(QColor("#536066"), 2)); painter.drawLine(margin, y, margin + width, y)
        colors = {"D": "#35c7d4", "I": "#a78bfa", "L": "#58d68d"}
        for index, row in enumerate(self.history):
            x = margin + (index * width / max(1, len(self.history) - 1)); code = str(row.get("backup_type", ""))
            painter.setBrush(QColor(colors.get(code, "#98a6aa"))); painter.setPen(Qt.PenStyle.NoPen)
            radius = 7 if code == "D" else 5 if code == "I" else 3; painter.drawEllipse(int(x - radius), y - radius, radius * 2, radius * 2)
        painter.setPen(QColor("#98a6aa")); painter.drawText(margin, 57, "Full"); painter.setPen(QColor("#a78bfa")); painter.drawText(margin + 38, 57, "Differential"); painter.setPen(QColor("#58d68d")); painter.drawText(margin + 112, 57, "Log")


class JobDurationTrendWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent); self.values: list[tuple[float, bool]] = []; self.next_run = ""; self.setFixedHeight(115)

    def set_history(self, rows: list[dict], next_run: str = "") -> None:
        self.values = [(float(row.get("duration_seconds", 0)) / 60, row.get("status") == "Succeeded") for row in reversed(rows[:40])]; self.next_run = next_run; self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self); painter.fillRect(self.rect(), QColor("#171c1f")); margin = 20
        if not self.values:
            painter.setPen(QColor("#98a6aa")); painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Select a job with execution history to view duration trend"); return
        high = max(1.0, max(value for value, _ok in self.values)); width = max(1, self.width() - margin * 2); height = max(1, self.height() - 38)
        points = []
        for index, (value, ok) in enumerate(self.values):
            x = margin + index * width / max(1, len(self.values) - 1); y = margin + height - value / high * height
            points.append((int(x), int(y), ok))
        painter.setPen(QPen(QColor("#35c7d4"), 2))
        for first, second in pairwise(points): painter.drawLine(first[0], first[1], second[0], second[1])
        for x, y, ok in points:
            painter.setBrush(QColor("#58d68d" if ok else "#ff6577")); painter.setPen(Qt.PenStyle.NoPen); painter.drawEllipse(x - 3, y - 3, 6, 6)
        painter.setPen(QColor("#98a6aa")); painter.drawText(margin, self.height() - 5, f"Duration minutes · peak {high:.1f}")
        if self.next_run and self.next_run != "—": painter.drawText(max(margin, self.width() - 210), self.height() - 5, f"Next scheduled: {self.next_run[:16].replace('T', ' ')}")


class HealthGauge(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.score = 0
        self.setFixedSize(116, 94)

    def set_score(self, score: int) -> None:
        self.score = max(0, min(100, score))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(13, 8, -13, 8)
        painter.setPen(QPen(QColor("#364044"), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 225 * 16, -270 * 16)
        color = (
            "#58d68d"
            if self.score >= HEALTHY_MINIMUM
            else "#f2b84b"
            if self.score >= ATTENTION_MINIMUM
            else "#ff6577"
        )
        painter.setPen(QPen(QColor(color), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 225 * 16, int(-270 * 16 * self.score / 100))
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.score))


class SqlMonitorDialog(QDialog):
    def __init__(self, profile: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = dict(profile or {})
        self.discovered_jobs = list(self.profile.get("jobs", []))
        self.setWindowTitle("SQL Server monitoring")
        self.setMinimumSize(560, 420)
        self.resize(760, 700)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(500)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 8, 2)
        content_layout.setSpacing(10)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name = QLineEdit(str(self.profile.get("name", "")))
        self.server = QLineEdit(str(self.profile.get("server", "")))
        self.database = QLineEdit(str(self.profile.get("database", "master")))
        self.driver = QLineEdit(
            str(self.profile.get("driver", "ODBC Driver 18 for SQL Server"))
        )
        self.auth = QComboBox()
        self.auth.addItem("Windows authentication", "windows")
        self.auth.addItem("SQL Server authentication", "sql")
        self.auth.setCurrentIndex(0 if self.profile.get("auth_mode", "windows") == "windows" else 1)
        self.username = QLineEdit(str(self.profile.get("username", "")))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(
            "Leave blank to keep the saved password" if self.profile.get("password_protected") else ""
        )
        self.encrypt = QCheckBox("Encrypt the SQL connection")
        self.encrypt.setChecked(bool(self.profile.get("encrypt", True)))
        self.trust = QCheckBox("Trust the server certificate")
        self.trust.setChecked(bool(self.profile.get("trust_certificate", True)))
        for label, widget in (
            ("Monitor name", self.name), ("SQL Server / instance", self.server),
            ("Database", self.database), ("ODBC driver", self.driver),
            ("Authentication", self.auth), ("SQL username", self.username),
            ("SQL password", self.password), ("Transport", self.encrypt),
            ("Certificate", self.trust),
        ):
            form.addRow(label, widget)
        content_layout.addLayout(form)
        test_row = QHBoxLayout()
        test_button = QPushButton("Test connection & discover jobs")
        test_button.setObjectName("primary")
        test_button.clicked.connect(self.test_connection)
        self.test_result = QLabel("Not tested")
        self.test_result.setWordWrap(True)
        test_row.addWidget(test_button)
        test_row.addWidget(self.test_result, 1)
        content_layout.addLayout(test_row)
        content_layout.addWidget(QLabel("SQL Agent jobs to monitor"))
        self.jobs = QListWidget()
        self.jobs.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self._populate_jobs(self.discovered_jobs)
        self.jobs.setMinimumHeight(90)
        self.jobs.setMaximumHeight(180)
        content_layout.addWidget(self.jobs)
        options = QFormLayout()
        options.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        options.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.notify_failure = QCheckBox("Email when a selected job fails, retries, or is canceled")
        self.notify_failure.setChecked(bool(self.profile.get("notify_failure", True)))
        self.notify_success = QCheckBox("Email when a selected job succeeds")
        self.notify_success.setChecked(bool(self.profile.get("notify_success", False)))
        self.notify_growth = QCheckBox("Email when allocated database size grows by")
        self.notify_growth.setChecked(bool(self.profile.get("notify_growth", False)))
        self.growth = QSpinBox()
        self.growth.setRange(1, 1_000_000)
        self.growth.setValue(int(self.profile.get("growth_notify_mb", 100)))
        self.growth.setSuffix(" MB")
        self.capacity_interval = QSpinBox()
        self.capacity_interval.setRange(5, 1440)
        self.capacity_interval.setValue(int(self.profile.get("capacity_interval_minutes", 15)))
        self.capacity_interval.setSuffix(" minutes")
        self.capacity_interval.setToolTip(
            "Capacity inventory is intentionally slower than health polling to limit SQL Server overhead."
        )
        self.backup_interval = QSpinBox()
        self.backup_interval.setRange(15, 1440)
        self.backup_interval.setValue(int(self.profile.get("backup_interval_minutes", 60)))
        self.backup_interval.setSuffix(" minutes")
        self.performance_interval = QSpinBox()
        self.performance_interval.setRange(1, 60)
        self.performance_interval.setValue(int(self.profile.get("performance_interval_minutes", 5)))
        self.performance_interval.setSuffix(" minutes")
        self.performance_interval.setToolTip("Uses bounded DMV queries and cumulative-counter deltas.")
        self.session_interval = QSpinBox()
        self.session_interval.setRange(15, 3600)
        self.session_interval.setValue(int(self.profile.get("session_interval_seconds", 60)))
        self.session_interval.setSuffix(" seconds")
        self.session_interval.setToolTip(
            "Refreshes the live user-session inventory and logs blocking participants."
        )
        self.configuration_interval = QSpinBox()
        self.configuration_interval.setRange(15, 1440)
        self.configuration_interval.setValue(
            int(self.profile.get("configuration_interval_minutes", 60))
        )
        self.configuration_interval.setSuffix(" minutes")
        self.configuration_interval.setToolTip(
            "Compares SQL Server and database settings with the preceding approved snapshot."
        )
        self.performance_sustain = QSpinBox()
        self.performance_sustain.setRange(1, 240)
        self.performance_sustain.setValue(int(self.profile.get("performance_sustain_minutes", 10)))
        self.performance_sustain.setSuffix(" minutes")
        self.performance_cpu = QSpinBox(); self.performance_cpu.setRange(1, 100)
        self.performance_cpu.setValue(int(self.profile.get("performance_cpu_percent", 80))); self.performance_cpu.setSuffix(" %")
        self.performance_grants = QSpinBox(); self.performance_grants.setRange(1, 10000)
        self.performance_grants.setValue(int(self.profile.get("performance_grants_pending", 1)))
        self.performance_blocking = QSpinBox(); self.performance_blocking.setRange(1, 86400)
        self.performance_blocking.setValue(int(self.profile.get("performance_blocked_seconds", 60))); self.performance_blocking.setSuffix(" seconds")
        self.performance_io = QSpinBox(); self.performance_io.setRange(1, 10000)
        self.performance_io.setValue(int(self.profile.get("performance_io_latency_ms", 20))); self.performance_io.setSuffix(" ms")
        self.long_query_seconds = QSpinBox(); self.long_query_seconds.setRange(5, 86400)
        self.long_query_seconds.setValue(int(self.profile.get("long_query_seconds", 60))); self.long_query_seconds.setSuffix(" seconds")
        self.require_full = QCheckBox("Require a recent full backup")
        self.require_full.setChecked(bool(self.profile.get("backup_require_full", True)))
        self.full_hours = QSpinBox(); self.full_hours.setRange(1, 720)
        self.full_hours.setValue(int(self.profile.get("backup_full_max_hours", 24))); self.full_hours.setSuffix(" hours")
        self.require_diff = QCheckBox("Require a recent differential backup")
        self.require_diff.setChecked(bool(self.profile.get("backup_require_differential", False)))
        self.require_log = QCheckBox("Require transaction-log backups (recommended for FULL recovery)")
        self.require_log.setChecked(bool(self.profile.get("backup_require_log", True)))
        self.log_minutes = QSpinBox(); self.log_minutes.setRange(5, 1440)
        self.log_minutes.setValue(int(self.profile.get("backup_log_max_minutes", 60))); self.log_minutes.setSuffix(" minutes")
        self.job_overdue = QSpinBox(); self.job_overdue.setRange(1, 720)
        self.job_overdue.setValue(int(self.profile.get("job_overdue_hours", 24))); self.job_overdue.setSuffix(" hours")
        self.job_failure_streak = QSpinBox(); self.job_failure_streak.setRange(2, 20)
        self.job_failure_streak.setValue(int(self.profile.get("job_failure_streak_threshold", 3))); self.job_failure_streak.setSuffix(" failures")
        self.job_schedule_grace = QSpinBox(); self.job_schedule_grace.setRange(0, 240)
        self.job_schedule_grace.setValue(int(self.profile.get("job_schedule_grace_minutes", 15))); self.job_schedule_grace.setSuffix(" minutes")
        self.job_long_running = QSpinBox(); self.job_long_running.setRange(1, 10080)
        self.job_long_running.setValue(int(self.profile.get("job_long_running_minutes", 120))); self.job_long_running.setSuffix(" minutes")
        self.allow_manual_job_run = QCheckBox("Allow confirmed manual execution of monitored jobs")
        self.allow_manual_job_run.setChecked(bool(self.profile.get("allow_manual_job_run", False)))
        self.allow_session_termination = QCheckBox("Allow confirmed termination of selected user sessions")
        self.allow_session_termination.setChecked(bool(self.profile.get("allow_session_termination", False)))
        self.enabled = QCheckBox("Enable this SQL monitor")
        self.enabled.setChecked(bool(self.profile.get("enabled", True)))
        options.addRow("Failure notifications", self.notify_failure)
        options.addRow("Success notifications", self.notify_success)
        options.addRow(self.notify_growth, self.growth)
        options.addRow("Capacity collection interval", self.capacity_interval)
        options.addRow("Backup evidence interval", self.backup_interval)
        options.addRow("Performance collection interval", self.performance_interval)
        options.addRow("Session and blocking interval", self.session_interval)
        options.addRow("Configuration snapshot interval", self.configuration_interval)
        options.addRow("Sustained performance window", self.performance_sustain)
        options.addRow("SQL CPU safety threshold", self.performance_cpu)
        options.addRow("Pending memory-grant threshold", self.performance_grants)
        options.addRow("Blocking duration threshold", self.performance_blocking)
        options.addRow("File I/O latency threshold", self.performance_io)
        options.addRow("Long-running query evidence after", self.long_query_seconds)
        options.addRow(self.require_full, self.full_hours)
        options.addRow("Differential SLA", self.require_diff)
        options.addRow(self.require_log, self.log_minutes)
        options.addRow("Job overdue after", self.job_overdue)
        options.addRow("Recurring failure escalation", self.job_failure_streak)
        options.addRow("Schedule grace period", self.job_schedule_grace)
        options.addRow("Long-running limit", self.job_long_running)
        options.addRow("Manual job action", self.allow_manual_job_run)
        options.addRow("Session termination", self.allow_session_termination)
        options.addRow("Monitoring", self.enabled)
        content_layout.addLayout(options)
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save SQL monitor")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        actions.addStretch(); actions.addWidget(cancel); actions.addWidget(save)
        layout.addLayout(actions)

    def _populate_jobs(self, jobs: list[str]) -> None:
        selected = set(self.profile.get("jobs", []))
        self.jobs.clear()
        for name in jobs:
            item = QListWidgetItem(name)
            item.setSelected(name in selected)
            self.jobs.addItem(item)

    def _values(self) -> dict:
        values = dict(self.profile)
        values.update({
            "id": values.get("id") or str(uuid.uuid4()), "name": self.name.text().strip(),
            "server": self.server.text().strip(), "database": self.database.text().strip() or "master",
            "driver": self.driver.text().strip() or "ODBC Driver 18 for SQL Server",
            "auth_mode": self.auth.currentData(), "username": self.username.text().strip(),
            "encrypt": self.encrypt.isChecked(), "trust_certificate": self.trust.isChecked(),
        })
        return values

    def test_connection(self) -> None:
        values = self._values()
        try:
            result = test_sql_connection(values, self.password.text())
        except Exception as exc:  # noqa: BLE001
            self.test_result.setText(f"Connection failed: {exc}")
            self.test_result.setObjectName("statusBad")
            return
        self.discovered_jobs = result["jobs"]
        self._populate_jobs(self.discovered_jobs)
        self.test_result.setText(f"Connected to {result['server']} · SQL {result['version']} · {len(result['jobs'])} job(s)")
        self.test_result.setObjectName("statusGood")

    def save(self) -> None:
        if not self.name.text().strip() or not self.server.text().strip():
            QMessageBox.warning(self, "Required values", "Enter a monitor name and SQL Server instance.")
            return
        if self.auth.currentData() == "sql" and not self.username.text().strip():
            QMessageBox.warning(self, "SQL login required", "Enter the SQL monitoring username.")
            return
        self.profile = self._values()
        if self.password.text():
            self.profile["password_protected"] = protect(self.password.text())
        self.profile.update({
            "jobs": [item.text() for item in self.jobs.selectedItems()],
            "notify_failure": self.notify_failure.isChecked(),
            "notify_success": self.notify_success.isChecked(),
            "notify_growth": self.notify_growth.isChecked(),
            "growth_notify_mb": self.growth.value(), "enabled": self.enabled.isChecked(),
            "capacity_interval_minutes": self.capacity_interval.value(),
            "backup_interval_minutes": self.backup_interval.value(),
            "performance_interval_minutes": self.performance_interval.value(),
            "session_interval_seconds": self.session_interval.value(),
            "configuration_interval_minutes": self.configuration_interval.value(),
            "performance_sustain_minutes": self.performance_sustain.value(),
            "performance_cpu_percent": self.performance_cpu.value(),
            "performance_grants_pending": self.performance_grants.value(),
            "performance_blocked_seconds": self.performance_blocking.value(),
            "performance_io_latency_ms": self.performance_io.value(),
            "long_query_seconds": self.long_query_seconds.value(),
            "backup_require_full": self.require_full.isChecked(),
            "backup_full_max_hours": self.full_hours.value(),
            "backup_require_differential": self.require_diff.isChecked(),
            "backup_require_log": self.require_log.isChecked(),
            "backup_log_max_minutes": self.log_minutes.value(),
            "job_overdue_hours": self.job_overdue.value(),
            "job_failure_streak_threshold": self.job_failure_streak.value(),
            "job_schedule_grace_minutes": self.job_schedule_grace.value(),
            "job_long_running_minutes": self.job_long_running.value(),
            "allow_manual_job_run": self.allow_manual_job_run.isChecked(),
            "allow_session_termination": self.allow_session_termination.isChecked(),
        })
        self.accept()


class RecoveryEvidenceDialog(QDialog):
    def __init__(self, database_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record recovery assurance evidence")
        self.resize(560, 310)
        layout = QVBoxLayout(self)
        title = QLabel(f"Recovery evidence · {database_name}")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold)); layout.addWidget(title)
        note = QLabel("Record an approved VERIFYONLY or isolated restore test. This is auditable evidence supplied by the engineer; the monitor does not run a restore automatically.")
        note.setWordWrap(True); note.setStyleSheet("color:#98a6aa"); layout.addWidget(note)
        form = QFormLayout()
        self.kind = QComboBox(); self.kind.addItems(["RESTORE VERIFYONLY", "Isolated restore test"])
        self.performed = QDateEdit(QDate.currentDate()); self.performed.setCalendarPopup(True)
        self.result = QComboBox(); self.result.addItems(["Succeeded", "Failed"])
        self.owner = QLineEdit(); self.owner.setPlaceholderText("Engineer or team")
        self.notes = QLineEdit(); self.notes.setPlaceholderText("Ticket, destination, duration, or evidence reference")
        self.destination = QLineEdit(); self.destination.setPlaceholderText("Isolated restore database/server or verification target")
        self.duration = QSpinBox(); self.duration.setRange(0, 100000); self.duration.setSuffix(" minutes"); self.duration.setSpecialValueText("Not recorded")
        form.addRow("Evidence type", self.kind); form.addRow("Performed date", self.performed)
        form.addRow("Result", self.result); form.addRow("Owner", self.owner); form.addRow("Destination", self.destination)
        form.addRow("Duration", self.duration); form.addRow("Evidence notes", self.notes)
        layout.addLayout(form)
        actions = QHBoxLayout(); cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        save = QPushButton("Record evidence"); save.setObjectName("primary"); save.clicked.connect(self._save)
        actions.addStretch(); actions.addWidget(cancel); actions.addWidget(save); layout.addLayout(actions)
        self.evidence: dict = {}

    def _save(self) -> None:
        if not self.owner.text().strip() or not self.notes.text().strip():
            QMessageBox.warning(self, "Evidence required", "Enter the evidence owner and an auditable reference or note.")
            return
        self.evidence = {
            "evidence_type": self.kind.currentText(),
            "performed_at": self.performed.date().toString("yyyy-MM-dd") + "T00:00:00",
            "result": self.result.currentText(), "owner": self.owner.text().strip(),
            "notes": self.notes.text().strip(), "destination": self.destination.text().strip(),
            "duration_minutes": self.duration.value() or None,
        }
        self.accept()


class BackupRuleDialog(QDialog):
    def __init__(self, rule: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rule = dict(rule or {})
        self.setWindowTitle("Backup monitor rule")
        self.resize(650, 430)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(str(self.rule.get("name", "")))
        self.path = QLineEdit(str(self.rule.get("path", "")))
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        folder = QPushButton("Browse folder…")
        file_button = QPushButton("Browse file…")
        folder.clicked.connect(self.browse_folder)
        file_button.clicked.connect(self.browse_file)
        path_row.addWidget(folder)
        path_row.addWidget(file_button)
        self.pattern = QLineEdit(str(self.rule.get("pattern", "*.bak")))
        self.keyword = QLineEdit(str(self.rule.get("keyword", "")))
        self.age = QSpinBox()
        self.age.setRange(0, 365)
        self.age.setValue(int(self.rule.get("max_age_days", 0)))
        self.age.setSuffix(" day(s)")
        self.check_time = QTimeEdit()
        self.check_time.setDisplayFormat("HH:mm")
        parsed = QTime.fromString(str(self.rule.get("check_time", "06:00")), "HH:mm")
        self.check_time.setTime(parsed if parsed.isValid() else QTime(6, 0))
        self.enabled = QCheckBox("Enable this backup rule")
        self.enabled.setChecked(bool(self.rule.get("enabled", True)))
        self.notify_found = QCheckBox("Send email when a valid backup is found")
        self.notify_found.setChecked(bool(self.rule.get("notify_found", False)))
        self.notify_missing = QCheckBox("Send email when missing, inaccessible, or too old")
        self.notify_missing.setChecked(bool(self.rule.get("notify_missing", True)))
        self.notify_recovery = QCheckBox("Send recovery email")
        self.notify_recovery.setChecked(bool(self.rule.get("notify_recovery", True)))
        form.addRow("Rule name", self.name)
        form.addRow("Folder or file", path_row)
        form.addRow("Filename pattern", self.pattern)
        form.addRow("Filename keyword", self.keyword)
        form.addRow("Maximum calendar age", self.age)
        form.addRow("Daily check time", self.check_time)
        form.addRow("Monitoring", self.enabled)
        form.addRow("Success notification", self.notify_found)
        form.addRow("Failure notification", self.notify_missing)
        form.addRow("Recovery notification", self.notify_recovery)
        layout.addLayout(form)
        actions = QHBoxLayout()
        save = QPushButton("Save rule")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def browse_folder(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Select backup folder", self.path.text())
        if value:
            self.path.setText(value)

    def browse_file(self) -> None:
        value, _ = QFileDialog.getOpenFileName(self, "Select backup file", self.path.text())
        if value:
            self.path.setText(value)

    def save(self) -> None:
        if not self.name.text().strip() or not self.path.text().strip():
            QMessageBox.warning(self, "Required values", "Enter a rule name and backup path.")
            return
        self.rule.update(
            {
                "id": self.rule.get("id") or str(uuid.uuid4()),
                "name": self.name.text().strip(),
                "path": self.path.text().strip(),
                "pattern": self.pattern.text().strip() or "*.bak",
                "keyword": self.keyword.text().strip(),
                "max_age_days": self.age.value(),
                "check_time": self.check_time.time().toString("HH:mm"),
                "enabled": self.enabled.isChecked(),
                "notify_found": self.notify_found.isChecked(),
                "notify_missing": self.notify_missing.isChecked(),
                "notify_recovery": self.notify_recovery.isChecked(),
            }
        )
        self.accept()


class FileMonitorDialog(QDialog):
    def __init__(self, rule: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rule = dict(rule or {})
        self.setWindowTitle("File integrity monitor")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(str(self.rule.get("name", "")))
        self.path = QLineEdit(str(self.rule.get("path", "")))
        row = QHBoxLayout()
        row.addWidget(self.path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        self.enabled = QCheckBox("Enable integrity monitoring")
        self.enabled.setChecked(bool(self.rule.get("enabled", True)))
        form.addRow("Monitor name", self.name)
        form.addRow("Specific file", row)
        form.addRow("Monitoring", self.enabled)
        note = QLabel(
            "Tracks creation, deletion, name, extension, size, modified time, and SHA-256 content."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#98a6aa")
        form.addRow("Captured baseline", note)
        layout.addLayout(form)
        actions = QHBoxLayout()
        save = QPushButton("Save monitor")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def browse(self) -> None:
        value, _ = QFileDialog.getOpenFileName(self, "Select file", self.path.text())
        if value:
            self.path.setText(value)

    def save(self) -> None:
        if not self.name.text().strip() or not self.path.text().strip():
            QMessageBox.warning(self, "Required values", "Enter a monitor name and file path.")
            return
        self.rule.update(
            {
                "id": self.rule.get("id") or str(uuid.uuid4()),
                "name": self.name.text().strip(),
                "path": self.path.text().strip(),
                "enabled": self.enabled.isChecked(),
            }
        )
        self.accept()


class ComponentChangesDialog(QDialog):
    def __init__(self, store: Store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Implemented component changes")
        self.resize(1150, 650)
        layout = QVBoxLayout(self)
        title = QLabel("Implemented component changes")
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        layout.addWidget(title)
        filters = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems(["Today", "This week", "Last week", "This month", "Custom"])
        self.component = QComboBox()
        self.component.addItems(["All", "Windows Service", "IIS Pool", "File"])
        self.from_date = QDateEdit(QDate.currentDate())
        self.to_date = QDateEdit(QDate.currentDate())
        for field in (self.from_date, self.to_date):
            field.setCalendarPopup(True)
            field.dateChanged.connect(self.refresh)
        self.period.currentTextChanged.connect(self._period_changed)
        self.component.currentTextChanged.connect(self.refresh)
        for label, widget in (
            ("Period", self.period),
            ("Component", self.component),
            ("From", self.from_date),
            ("To", self.to_date),
        ):
            filters.addWidget(QLabel(label))
            filters.addWidget(widget)
        refresh = QPushButton("Refresh now")
        refresh.clicked.connect(self.refresh)
        filters.addWidget(refresh)
        filters.addStretch()
        layout.addLayout(filters)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Status", "Change time", "Type", "Item", "Setting", "From", "To", "Resolution"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for column in (3, 4, 5, 6):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        resolved = QPushButton("Resolved")
        resolved.clicked.connect(self.resolve_selected)
        accept = QPushButton("Accept current baseline")
        accept.setObjectName("primary")
        accept.clicked.connect(self.accept_baseline)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(resolved)
        actions.addWidget(accept)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(10_000)
        self._period_changed(self.period.currentText())

    def _period_changed(self, period: str) -> None:
        custom = period == "Custom"
        self.from_date.setEnabled(custom)
        self.to_date.setEnabled(custom)
        self.refresh()

    def _range(self) -> tuple[str, str]:
        today = datetime.now().astimezone().date()
        period = self.period.currentText()
        if period == "Today":
            start, end = today, today + timedelta(days=1)
        elif period == "This week":
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=7)
        elif period == "Last week":
            end = today - timedelta(days=today.weekday())
            start = end - timedelta(days=7)
        elif period == "This month":
            start = today.replace(day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            start = self.from_date.date().toPython()
            end = self.to_date.date().toPython() + timedelta(days=1)
        zone = datetime.now().astimezone().tzinfo
        return (
            datetime.combine(start, datetime.min.time(), zone).astimezone(UTC).isoformat(),
            datetime.combine(end, datetime.min.time(), zone).astimezone(UTC).isoformat(),
        )

    def refresh(self) -> None:
        since, until = self._range()
        rows = self.store.component_changes(
            since=since, until=until, component_type=self.component.currentText()
        )
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            status = QTableWidgetItem(str(row["status"]).title())
            status.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            status.setData(Qt.ItemDataRole.UserRole + 1, str(row["item_key"]))
            self.table.setItem(index, 0, status)
            values = (
                display_server_time(self.store, row["detected_at"]),
                str(row["component_type"]),
                str(row["item_name"]),
                friendly_property_name(str(row["property_name"])),
                str(row["old_value"] or "—"),
                str(row["new_value"] or "—"),
                str(row["resolution"] or "—"),
            )
            for column, value in enumerate(values, 1):
                self.table.setItem(index, column, QTableWidgetItem(value))

    def _selected(self) -> tuple[int, str] | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select change", "Select a change first.")
            return None
        item = self.table.item(row, 0)
        return int(item.data(Qt.ItemDataRole.UserRole)), str(
            item.data(Qt.ItemDataRole.UserRole + 1)
        )

    def resolve_selected(self) -> None:
        selected = self._selected()
        if selected and self.store.resolve_component_change(
            selected[0], "Resolved manually by engineer"
        ):
            self.store.add_event("info", "change", selected[1], "Component change resolved")
            self.refresh()

    def accept_baseline(self) -> None:
        selected = self._selected()
        if not selected:
            return
        item_key = selected[1]
        current = self.store.get("component_current_snapshots", {})
        if item_key not in current:
            QMessageBox.warning(self, "Snapshot unavailable", "No current snapshot is available.")
            return
        baselines = self.store.get("component_baselines", {})
        baselines[item_key] = current[item_key]
        self.store.set("component_baselines", baselines)
        self.store.resolve_component_changes_for_item(item_key, "New baseline accepted by engineer")
        self.store.add_event("info", "change", item_key, "New component baseline accepted")
        self.refresh()


class ScoreIncidentsDialog(QDialog):
    def __init__(self, store: Store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Health score incidents")
        self.resize(980, 600)
        layout = QVBoxLayout(self)
        title = QLabel("Health score incidents")
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        layout.addWidget(title)
        description = QLabel(
            "Review automatic and recurring deductions. Select an open incident and click "
            "Resolved to remove its deduction from the health score."
        )
        description.setWordWrap(True)
        description.setStyleSheet("color:#98a6aa")
        layout.addWidget(description)

        filters = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems(["Today", "This week", "Last week", "This month", "Custom"])
        self.period.currentTextChanged.connect(self._period_changed)
        self.from_date = QDateEdit(QDate.currentDate())
        self.from_date.setCalendarPopup(True)
        self.to_date = QDateEdit(QDate.currentDate())
        self.to_date.setCalendarPopup(True)
        self.from_date.dateChanged.connect(self.refresh)
        self.to_date.dateChanged.connect(self.refresh)
        filters.addWidget(QLabel("Period"))
        filters.addWidget(self.period)
        filters.addWidget(QLabel("From"))
        filters.addWidget(self.from_date)
        filters.addWidget(QLabel("To"))
        filters.addWidget(self.to_date)
        filters.addStretch()
        layout.addLayout(filters)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Status", "Opened", "Target", "Reason", "Deduction", "Occurrences", "Resolution"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        resolved = QPushButton("Resolved")
        resolved.setObjectName("primary")
        resolved.clicked.connect(self.resolve_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(resolved)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)
        self._period_changed(self.period.currentText())

    def _period_changed(self, period: str) -> None:
        custom = period == "Custom"
        self.from_date.setEnabled(custom)
        self.to_date.setEnabled(custom)
        self.refresh()

    def _range(self) -> tuple[str, str]:
        today = datetime.now().astimezone().date()
        period = self.period.currentText()
        if period == "Today":
            start, end = today, today + timedelta(days=1)
        elif period == "This week":
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=7)
        elif period == "Last week":
            end = today - timedelta(days=today.weekday())
            start = end - timedelta(days=7)
        elif period == "This month":
            start = today.replace(day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            start = self.from_date.date().toPython()
            end = self.to_date.date().toPython() + timedelta(days=1)
        local_zone = datetime.now().astimezone().tzinfo
        start_utc = datetime.combine(start, datetime.min.time(), local_zone).astimezone(UTC)
        end_utc = datetime.combine(end, datetime.min.time(), local_zone).astimezone(UTC)
        return start_utc.isoformat(timespec="seconds"), end_utc.isoformat(timespec="seconds")

    def refresh(self) -> None:
        since, until = self._range()
        rows = self.store.score_incidents(since=since, until=until)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            status = QTableWidgetItem(str(row["status"]).title())
            status.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            self.table.setItem(index, 0, status)
            self.table.setItem(index, 1, QTableWidgetItem(display_server_time(self.store, row["opened_at"])))
            self.table.setItem(index, 2, QTableWidgetItem(str(row["target"])))
            self.table.setItem(index, 3, QTableWidgetItem(str(row["title"])))
            self.table.setItem(index, 4, QTableWidgetItem(f"-{row['deduction']}%"))
            self.table.setItem(index, 5, QTableWidgetItem(str(row["occurrences"])))
            self.table.setItem(index, 6, QTableWidgetItem(str(row["resolution"] or "—")))

    def resolve_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select incident", "Select an incident first.")
            return
        status_item = self.table.item(row, 0)
        if status_item.text().lower() != "open":
            QMessageBox.information(self, "Already resolved", "This incident is already resolved.")
            return
        incident_id = int(status_item.data(Qt.ItemDataRole.UserRole))
        target = self.table.item(row, 2).text()
        if self.store.resolve_score_incident(incident_id, "Resolved manually by engineer"):
            self.store.add_event(
                "info", "incident", target, "Health score incident resolved manually"
            )
            self.refresh()
            if self.parent() and hasattr(self.parent(), "refresh_dashboard"):
                self.parent().refresh_dashboard()


class TargetHistoryDialog(QDialog):
    def __init__(self, store: Store, target: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Incident history · {target}")
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        title = QLabel(target)
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        layout.addWidget(title)
        layout.addWidget(
            QLabel("Failures, automatic actions, configuration changes, and recoveries")
        )
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Time", "Severity", "Category", "What happened"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        rows = store.target_events(target)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, value in enumerate(
                (
                    display_server_time(self.store, row["created_at"]),
                    row["severity"],
                    row["category"],
                    row["message"],
                )
            ):
                table.setItem(row_index, column, QTableWidgetItem(str(value)))
        layout.addWidget(table, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class DiscoveryDialog(QDialog):
    """Search server inventory and add selected targets to MiMonitoringService."""

    def __init__(self, store: Store, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store, self.kind = store, kind
        title = "Windows services" if kind == "service" else "IIS application pools"
        self.setWindowTitle(f"Add {title}")
        self.resize(760, 590)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        heading = QLabel(f"Add {title}")
        heading.setFont(QFont("Segoe UI", 19, QFont.Weight.Bold))
        outer.addWidget(heading)
        detail = QLabel(
            "Search the detected inventory, select items to protect, then add them. "
            "Existing configurations are preserved."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet("color:#98a6aa")
        outer.addWidget(detail)
        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Search {title} by display or technical name…")
        self.search.textChanged.connect(self.filter_items)
        outer.addWidget(self.search)
        self.summary = QLabel("Scanning server…")
        self.summary.setStyleSheet("color:#98a6aa")
        outer.addWidget(self.summary)
        self.items = QListWidget()
        self.items.setAlternatingRowColors(True)
        outer.addWidget(self.items, 1)
        buttons = QHBoxLayout()
        refresh = QPushButton("Scan again")
        cancel = QPushButton("Cancel")
        add = QPushButton("Add selected")
        add.setObjectName("primary")
        refresh.clicked.connect(self.scan)
        cancel.clicked.connect(self.reject)
        add.clicked.connect(self.add_selected)
        buttons.addWidget(refresh)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(add)
        outer.addLayout(buttons)
        self.scan()

    def scan(self) -> None:
        self.items.clear()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            discovered = windows_services() if self.kind == "service" else iis_pools()
        finally:
            QApplication.restoreOverrideCursor()
        existing = {row["target_key"] for row in self.store.targets(self.kind)}
        for target in discovered:
            item = QListWidgetItem(
                f"{target['name']}    ·    {target['status']}"
                + ("    ·    already added" if target["key"] in existing else "")
            )
            item.setData(Qt.ItemDataRole.UserRole, target)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if target["key"] in existing else Qt.CheckState.Unchecked
            )
            self.items.addItem(item)
        if discovered:
            self.summary.setText(f"{len(discovered)} detected · {len(existing)} already added")
        elif self.kind == "pool":
            self.summary.setText(
                "No IIS pools detected. Confirm IIS is installed and MiMonitoringService is running as administrator."
            )
        else:
            self.summary.setText("No Windows services could be detected on this server.")

    def filter_items(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.items.count()):
            item = self.items.item(index)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def add_selected(self) -> None:
        added = 0
        for index in range(self.items.count()):
            entry = self.items.item(index)
            if entry.checkState() != Qt.CheckState.Checked:
                continue
            target = entry.data(Qt.ItemDataRole.UserRole)
            if self.store.target(self.kind, target["key"]) is None:
                self.store.upsert_target(
                    self.kind,
                    target["key"],
                    target["name"],
                    {"auto_restart": True, "retry_count": 3},
                )
                added += 1
        self.store.add_event(
            "info", "configuration", self.kind, f"{added} {self.kind} item(s) added"
        )
        self.accept()


class RelatedServicesDialog(QDialog):
    def __init__(
        self, main_service: str, selected: set[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.main_service = main_service
        self.selected_services = set(selected)
        self.setWindowTitle(f"Related services · {main_service}")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("Select related services")
        title.setFont(QFont("Segoe UI", 19, QFont.Weight.Bold))
        layout.addWidget(title)
        help_text = QLabel(
            "Manually select services that must stop before the main service restarts. "
            "Only selected services that are running will be stopped and restored."
        )
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color:#98a6aa")
        layout.addWidget(help_text)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by display or technical service name…")
        self.search.textChanged.connect(self.filter_items)
        layout.addWidget(self.search)
        self.count_label = QLabel()
        self.count_label.setStyleSheet("color:#45ced9;font-weight:600")
        layout.addWidget(self.count_label)
        self.items = QListWidget()
        self.items.itemChanged.connect(self.update_count)
        layout.addWidget(self.items, 1)
        tools = QHBoxLayout()
        select_visible = QPushButton("Select visible")
        clear_visible = QPushButton("Clear visible")
        select_visible.clicked.connect(lambda: self.set_visible_check_state(Qt.CheckState.Checked))
        clear_visible.clicked.connect(lambda: self.set_visible_check_state(Qt.CheckState.Unchecked))
        tools.addWidget(select_visible)
        tools.addWidget(clear_visible)
        tools.addStretch()
        layout.addLayout(tools)
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        save = QPushButton("Save selection")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.save_selection)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        self.scan()

    def scan(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            services = windows_services()
        finally:
            QApplication.restoreOverrideCursor()
        self.items.blockSignals(True)
        self.items.clear()
        for service in services:
            if service["key"] == self.main_service:
                continue
            item = QListWidgetItem(f"{service['name']}    ·    {service['status']}")
            item.setData(Qt.ItemDataRole.UserRole, service["key"])
            item.setToolTip(service["key"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if service["key"] in self.selected_services
                else Qt.CheckState.Unchecked
            )
            self.items.addItem(item)
        self.items.blockSignals(False)
        self.update_count()

    def filter_items(self, text: str) -> None:
        SetupWizard.filter_list(self.items, text)

    def set_visible_check_state(self, state: Qt.CheckState) -> None:
        for index in range(self.items.count()):
            item = self.items.item(index)
            if not item.isHidden():
                item.setCheckState(state)

    def update_count(self) -> None:
        count = sum(
            self.items.item(index).checkState() == Qt.CheckState.Checked
            for index in range(self.items.count())
        )
        self.count_label.setText(f"{count} related service(s) selected")

    def save_selection(self) -> None:
        self.selected_services = {
            str(self.items.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.items.count())
            if self.items.item(index).checkState() == Qt.CheckState.Checked
        }
        self.accept()


class SetupWizard(QDialog):
    def __init__(self, store: Store, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.setWindowTitle(f"Welcome · MiMonitoringService v{__version__}")
        self.setMinimumSize(760, 560)
        self.page = 0
        self.pages = QStackedWidget()
        self.step_names = [
            "Welcome", "Identity", "Email", "Services", "IIS Pools", "Disks", "Performance", "Review"
        ]
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color:#45ced9;font-weight:600")
        config = self.store.get("configuration")
        self.operator_email = QLineEdit(config["client"].get("operator_email", ""))
        self.operator_email.setPlaceholderText("your.name@millensys.com")
        self.client_name = QLineEdit(config["client"].get("name", ""))
        self.client_name.setPlaceholderText("Example: Millensys Cairo")
        self.site = QLineEdit(config["client"].get("site", ""))
        self.site.setPlaceholderText("Datacenter, branch, or customer location")
        self.server_label = QLineEdit(config["client"].get("server_label", ""))
        self.server_label.setPlaceholderText("Friendly name shown in alerts")
        self.smtp_host = QLineEdit(config["email"].get("smtp_server", "smtp.gmail.com"))
        self.smtp_port = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(int(config["email"].get("smtp_port", 587)))
        self.smtp_user = QLineEdit(
            config["email"].get("username", "millensysnotifications@gmail.com")
        )
        self.sender_name = QLineEdit(
            config["email"].get("sender_name", "MiMonitoringService")
        )
        self.sender_name.setPlaceholderText("Name displayed in notification emails")
        self.smtp_password = QLineEdit()
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_password.setPlaceholderText(
            "Already protected on this server"
            if config["email"].get("password_protected")
            else "Gmail app password · encrypted with Windows DPAPI"
        )
        self.recipients = QLineEdit(", ".join(config["email"].get("recipients", [])))
        self.recipients.setPlaceholderText("operator@millensys.com, support@millensys.com")
        self.cc = QLineEdit(", ".join(config["email"].get("cc", [])))
        self.cc.setPlaceholderText("Optional CC addresses, comma separated")
        self.service_list = QListWidget()
        self.pool_list = QListWidget()
        self.disk_list = QListWidget()
        self.wizard_monitor_all_disks = QCheckBox("Monitor every detected drive")
        self.wizard_monitor_all_disks.setChecked(config["disk"].get("monitor_all", True))
        self.wizard_disk_percent = QSpinBox()
        self.wizard_disk_percent.setRange(1, 99)
        self.wizard_disk_percent.setValue(config["disk"].get("minimum_free_percent", 15))
        self.wizard_disk_percent.setSuffix(" % free")
        self.wizard_disk_mb = QSpinBox()
        self.wizard_disk_mb.setRange(100, 2_000_000)
        self.wizard_disk_mb.setValue(config["disk"].get("minimum_free_mb", 5120))
        self.wizard_disk_mb.setSuffix(" MB free")
        self.wizard_cpu = QSpinBox()
        self.wizard_cpu.setRange(1, 100)
        self.wizard_cpu.setValue(config["performance"]["cpu_percent"])
        self.wizard_cpu.setSuffix(" %")
        self.wizard_memory = QSpinBox()
        self.wizard_memory.setRange(1, 100)
        self.wizard_memory.setValue(config["performance"]["memory_percent"])
        self.wizard_memory.setSuffix(" %")
        self.wizard_sustain = QSpinBox()
        self.wizard_sustain.setRange(10, 3600)
        self.wizard_sustain.setValue(config["performance"].get("sustain_seconds", 120))
        self.wizard_sustain.setSuffix(" seconds")
        self.pages.addWidget(self._welcome_page())
        self.pages.addWidget(self._client_page())
        self.pages.addWidget(self._email_page())
        self.pages.addWidget(self._target_selection_page("Windows Services", "service"))
        self.pages.addWidget(self._target_selection_page("IIS Application Pools", "pool"))
        self.pages.addWidget(self._disk_page())
        self.pages.addWidget(self._performance_page())
        self.pages.addWidget(self._finish_page())
        self.back = QPushButton("Back")
        self.back.clicked.connect(self.go_back)
        self.next = QPushButton("Next")
        self.next.setObjectName("primary")
        self.next.clicked.connect(self.go_next)
        buttons = QHBoxLayout()
        guide = QPushButton("Offline user guide")
        guide.clicked.connect(lambda: open_user_guide(self))
        buttons.addWidget(guide)
        buttons.addWidget(self.back)
        buttons.addStretch()
        buttons.addWidget(self.next)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(42, 36, 42, 30)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.pages)
        layout.addLayout(buttons)
        copyright_label = QLabel(COPYRIGHT)
        copyright_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        copyright_label.setStyleSheet("color:#78858a;font-size:9pt")
        layout.addWidget(copyright_label)
        self._update_buttons()

    def _welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        title = QLabel("Your server, watched intelligently.")
        title.setFont(QFont("Segoe UI", 27, QFont.Weight.Bold))
        title.setWordWrap(True)
        body = QLabel(
            "MiMonitoringService watches Windows Services, IIS pools, disks, CPU and memory—then records what happened, attempts carefully guarded recovery, and tells the right people."
        )
        body.setWordWrap(True)
        body.setStyleSheet("color:#adb9bd;font-size:13pt;line-height:1.5")
        layout.addWidget(QLabel(f"MILLENSYS  ·  FIRST-RUN GUIDE  ·  v{__version__}"))
        layout.addWidget(title)
        layout.addSpacing(16)
        layout.addWidget(body)
        layout.addStretch()
        return page

    def _form_page(self, title: str, subtitle: str, rows: list[tuple[str, QWidget]]) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        heading = QLabel(title)
        heading.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        outer.addWidget(heading)
        detail = QLabel(subtitle)
        detail.setWordWrap(True)
        detail.setStyleSheet("color:#98a6aa")
        outer.addWidget(detail)
        outer.addSpacing(22)
        form = QFormLayout()
        form.setSpacing(14)
        for label, field in rows:
            form.addRow(label, field)
        outer.addLayout(form)
        outer.addStretch()
        return page

    def _client_page(self) -> QWidget:
        return self._form_page(
            "Identify this server",
            "These labels make dashboard entries and alerts meaningful to the recipient.",
            [
                ("Client name", self.client_name),
                ("Site / branch", self.site),
                ("Server label", self.server_label),
                ("Millensys operator", self.operator_email),
            ],
        )

    def _email_page(self) -> QWidget:
        return self._form_page(
            "Configure notifications",
            "Credentials are encrypted for this Windows machine. Use a provider-specific app password, never an account password.",
            [
                ("SMTP server", self.smtp_host),
                ("Port", self.smtp_port),
                ("Username", self.smtp_user),
                ("Sender name", self.sender_name),
                ("App password", self.smtp_password),
                ("To", self.recipients),
                ("CC", self.cc),
            ],
        )

    def _target_selection_page(self, title: str, kind: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel(f"Choose {title}")
        heading.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        layout.addWidget(heading)
        detail = QLabel(
            "Only checked items are monitored. You can enable, disable, or reconfigure them later."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet("color:#98a6aa")
        layout.addWidget(detail)
        search = QLineEdit()
        search.setPlaceholderText(f"Filter {title.lower()} by name…")
        target_list = self.service_list if kind == "service" else self.pool_list
        search.textChanged.connect(lambda text, widget=target_list: self.filter_list(widget, text))
        layout.addWidget(search)
        discover = QPushButton(f"Discover {title}")
        discover.setObjectName("primary")
        discover.clicked.connect(lambda: self.discover_wizard_targets(kind))
        layout.addWidget(discover)
        configure = QPushButton("Configure checked items")
        configure.setToolTip(
            "Complete recovery, API check, schedule, maintenance, and action settings for each checked item"
        )
        configure.clicked.connect(lambda: self.configure_wizard_targets(kind))
        layout.addWidget(configure)
        target_list.itemDoubleClicked.connect(
            lambda item: self.configure_wizard_target(kind, item)
        )
        layout.addWidget(target_list, 1)
        return page

    @staticmethod
    def filter_list(widget: QListWidget, text: str) -> None:
        needle = text.strip().lower()
        for index in range(widget.count()):
            item = widget.item(index)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _disk_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Choose disk monitoring")
        heading.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        layout.addWidget(heading)
        layout.addWidget(self.wizard_monitor_all_disks)
        thresholds = QHBoxLayout()
        thresholds.addWidget(QLabel("Alert below"))
        thresholds.addWidget(self.wizard_disk_percent)
        thresholds.addWidget(QLabel("or"))
        thresholds.addWidget(self.wizard_disk_mb)
        thresholds.addStretch()
        layout.addLayout(thresholds)
        discover = QPushButton("Discover drives")
        discover.setObjectName("primary")
        discover.clicked.connect(lambda: self.discover_wizard_targets("disk"))
        layout.addWidget(discover)
        layout.addWidget(self.disk_list, 1)
        return page

    def _performance_page(self) -> QWidget:
        return self._form_page(
            "Configure server performance",
            "Short spikes are ignored. Alerts begin only after utilization remains above the threshold.",
            [
                ("CPU threshold", self.wizard_cpu),
                ("Memory threshold", self.wizard_memory),
                ("Sustained duration", self.wizard_sustain),
            ],
        )

    def _finish_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch()
        title = QLabel("Ready to protect this server")
        title.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        layout.addWidget(title)
        self.finish_summary = QLabel()
        self.finish_summary.setWordWrap(True)
        self.finish_summary.setStyleSheet("color:#adb9bd;font-size:12pt")
        layout.addWidget(self.finish_summary)
        layout.addStretch()
        return page

    def go_back(self) -> None:
        self.page = max(0, self.page - 1)
        self.pages.setCurrentIndex(self.page)
        self._update_buttons()

    def go_next(self) -> None:
        if self.page == 1:
            if not self.client_name.text().strip():
                QMessageBox.warning(self, "Client name required", "Enter the client name.")
                return
            result = validate_operator(
                self.operator_email.text(), self.store.get("configuration")["registration"]
            )
            if not result.valid:
                QMessageBox.warning(self, "Millensys access required", result.message)
                return
            if not self.recipients.text().strip():
                self.recipients.setText(self.operator_email.text().strip().lower())
        if self.page == 2:
            if not self.smtp_host.text().strip() or not self.smtp_user.text().strip():
                QMessageBox.warning(
                    self, "SMTP configuration required", "Enter the SMTP server and username."
                )
                return
            recipient_entries = _email_entries(self.recipients.text())
            recipient_result = validate_recipients(
                recipient_entries, self.store.get("configuration")["registration"]
            )
            if not recipient_result.valid:
                QMessageBox.warning(
                    self, "Millensys recipient required", recipient_result.message
                )
                return
            existing = self.store.get("configuration")["email"].get("password_protected")
            if not self.smtp_password.text() and not existing:
                QMessageBox.warning(
                    self,
                    "SMTP credential required",
                    "Enter the Gmail app password or provision it securely before continuing.",
                )
                return
        if self.page < self.pages.count() - 1:
            self.page += 1
            if self.page == self.pages.count() - 1:
                self.update_finish_summary()
            self.pages.setCurrentIndex(self.page)
            self._update_buttons()
            return
        self.save_and_close()

    def _update_buttons(self) -> None:
        self.back.setEnabled(self.page > 0)
        self.progress_label.setText(
            f"STEP {self.page + 1} OF {self.pages.count()}   ·   {self.step_names[self.page]}"
        )
        self.next.setText(
            "Save and start MiMonitoringService" if self.page == self.pages.count() - 1 else "Next"
        )

    def discover_wizard_targets(self, kind: str) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if kind == "service":
                items, widget = windows_services(), self.service_list
            elif kind == "pool":
                items, widget = iis_pools(), self.pool_list
            else:
                items, widget = disks(), self.disk_list
        finally:
            QApplication.restoreOverrideCursor()
        selected = {row["target_key"]: bool(row["enabled"]) for row in self.store.targets(kind)}
        widget.clear()
        for item in items:
            entry = QListWidgetItem(f"{item['name']}    ·    {item.get('status', '')}")
            entry.setData(Qt.ItemDataRole.UserRole, item)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = selected.get(item["key"], False)
            entry.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            widget.addItem(entry)

    def configure_wizard_target(self, kind: str, entry: QListWidgetItem) -> None:
        item = entry.data(Qt.ItemDataRole.UserRole)
        if not item:
            return
        entry.setCheckState(Qt.CheckState.Checked)
        if self.store.target(kind, item["key"]) is None:
            self.store.upsert_target(
                kind, item["key"], item["name"], {"auto_restart": True, "retry_count": 3}
            )
        TargetConfigDialog(self.store, kind, item["key"], self).exec()

    def configure_wizard_targets(self, kind: str) -> None:
        widget = self.service_list if kind == "service" else self.pool_list
        checked = [
            widget.item(index)
            for index in range(widget.count())
            if widget.item(index).checkState() == Qt.CheckState.Checked
        ]
        if not checked:
            QMessageBox.information(
                self, "No items selected", "Check at least one item before configuring it."
            )
            return
        for entry in checked:
            self.configure_wizard_target(kind, entry)

    def update_finish_summary(self) -> None:
        counts = {}
        for kind, widget in (("services", self.service_list), ("IIS pools", self.pool_list), ("drives", self.disk_list)):
            counts[kind] = sum(
                widget.item(index).checkState() == Qt.CheckState.Checked
                for index in range(widget.count())
            )
        self.finish_summary.setText(
            f"Configuration review\n\n"
            f"{counts['services']} Windows service(s) · {counts['IIS pools']} IIS pool(s) · "
            f"{counts['drives']} selected drive(s)\n"
            f"Disk alerts: below {self.wizard_disk_percent.value()}% or "
            f"{self.wizard_disk_mb.value():,} MB free\n"
            f"Performance alerts: CPU {self.wizard_cpu.value()}% · memory "
            f"{self.wizard_memory.value()}% sustained for {self.wizard_sustain.value()} seconds\n\n"
            "MiMonitoringService will save these settings, install and start its Windows service, "
            "then open the dashboard. Double-click a selected service or pool on earlier steps to "
            "review its individual configuration."
        )

    def save_and_close(self) -> None:
        config = self.store.get("configuration")
        recipient_entries = _email_entries(self.recipients.text())
        recipients = _emails(self.recipients.text())
        recipient_result = validate_recipients(recipient_entries, config["registration"])
        if not recipient_result.valid:
            QMessageBox.warning(self, "Millensys recipient required", recipient_result.message)
            return
        config["setup_complete"] = False
        config["client"] = {
            "name": self.client_name.text().strip(),
            "site": self.site.text().strip(),
            "server_label": self.server_label.text().strip(),
            "operator_email": self.operator_email.text().strip().lower(),
        }
        email = config["email"]
        email.update(
            {
                "smtp_server": self.smtp_host.text().strip(),
                "smtp_port": self.smtp_port.value(),
                "username": self.smtp_user.text().strip(),
                "sender_name": self.sender_name.text().strip() or "MiMonitoringService",
                "recipients": recipients,
                "cc": _emails(self.cc.text()),
            }
        )
        if self.smtp_password.text():
            try:
                email["password_protected"] = protect(self.smtp_password.text())
            except RuntimeError as exc:
                QMessageBox.critical(self, "Cannot protect credential", str(exc))
                return
        config["disk"]["monitor_all"] = self.wizard_monitor_all_disks.isChecked()
        config["disk"]["minimum_free_percent"] = self.wizard_disk_percent.value()
        config["disk"]["minimum_free_mb"] = self.wizard_disk_mb.value()
        config["performance"]["cpu_percent"] = self.wizard_cpu.value()
        config["performance"]["memory_percent"] = self.wizard_memory.value()
        config["performance"]["sustain_seconds"] = self.wizard_sustain.value()
        for kind, widget in (
            ("service", self.service_list),
            ("pool", self.pool_list),
            ("disk", self.disk_list),
        ):
            for index in range(widget.count()):
                entry = widget.item(index)
                item = entry.data(Qt.ItemDataRole.UserRole)
                if entry.checkState() == Qt.CheckState.Checked:
                    self.store.upsert_target(
                        kind,
                        item["key"],
                        item["name"],
                        {"auto_restart": True, "retry_count": 3} if kind != "disk" else {},
                    )
                    self.store.set_target_enabled(kind, item["key"], True)
                elif self.store.target(kind, item["key"]) is not None:
                    self.store.set_target_enabled(kind, item["key"], False)
        self.store.set("configuration", config)
        service_state = agent_service_status()
        if service_state == "Not installed":
            ok, message = manage_agent_service("install")
            if not ok:
                QMessageBox.critical(self, "Service installation failed", message)
                return
            service_state = "Stopped"
        if service_state != "Running":
            ok, message = manage_agent_service("start")
            if not ok:
                QMessageBox.critical(self, "Service startup failed", message)
                return
        config["setup_complete"] = True
        self.store.set("configuration", config)
        self.store.add_event("info", "configuration", "setup", "First-run setup completed")
        self.accept()


def _emails(value: str) -> list[str]:
    candidates = _email_entries(value)
    pattern = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    return [item.lower() for item in candidates if pattern.fullmatch(item)]


def _email_entries(value: str) -> list[str]:
    return [
        item.strip().lower()
        for item in value.replace(";", ",").split(",")
        if item.strip()
    ]


class TargetConfigDialog(QDialog):
    """Edit one monitored item without exposing raw JSON to an operator."""

    def __init__(self, store: Store, kind: str, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store, self.kind, self.key = store, kind, key
        row = store.target(kind, key)
        if row is None:
            raise ValueError(f"Unknown {kind} target: {key}")
        self.config = json.loads(row["config"] or "{}")
        self.setWindowTitle(f"Configure · {row['display_name']}")
        self.setMinimumSize(680, 560)
        outer = QVBoxLayout(self)
        title = QLabel(row["display_name"])
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        outer.addWidget(title)
        detail = QLabel(
            f"{kind.title()} · {key}\nChanges are applied by the monitoring service on its next cycle."
        )
        detail.setStyleSheet("color:#98a6aa")
        outer.addWidget(detail)
        tabs = QTabWidget()
        tabs.addTab(self._recovery_tab(), "Recovery")
        tabs.addTab(self._api_tab(), "API check")
        tabs.addTab(self._schedule_tab(), "Schedule")
        tabs.addTab(self._maintenance_tab(), "Maintenance")
        outer.addWidget(tabs, 1)
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        save = QPushButton("Save configuration")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.save)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        outer.addLayout(buttons)

    def _recovery_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(22, 24, 22, 24)
        form.setSpacing(16)
        stopped_label = (
            "Start this Windows service automatically if it is stopped"
            if self.kind == "service"
            else "Start this IIS application pool automatically if it is stopped"
        )
        self.start_if_stopped = QCheckBox(stopped_label)
        self.start_if_stopped.setChecked(
            self.config.get("start_if_stopped", self.config.get("auto_restart", True))
        )
        self.start_if_stopped.setToolTip(
            "When monitoring detects a stopped item, attempt to start it using the retry policy below."
        )
        self.retry_count = QSpinBox()
        self.retry_count.setRange(1, 10)
        self.retry_count.setValue(int(self.config.get("retry_count", 3)))
        self.retry_count.setToolTip(
            "Attempt recovery this many times, then escalate by email if still unhealthy."
        )
        self.retry_delay = QSpinBox()
        self.retry_delay.setRange(10, 3600)
        self.retry_delay.setValue(int(self.config.get("retry_delay_seconds", 60)))
        self.retry_delay.setSuffix(" seconds")
        form.addRow("Stopped-state action", self.start_if_stopped)
        form.addRow("Recovery attempts", self.retry_count)
        form.addRow("Delay between recovery attempts", self.retry_delay)
        if self.kind == "service":
            self.related_selected = set(self.config.get("related_services", []))
            self.related_summary = QLabel()
            self.related_summary.setStyleSheet("color:#98a6aa")
            self.update_related_summary()
            edit_related = QPushButton("Edit related services…")
            edit_related.clicked.connect(self.edit_related_services)
            form.addRow("Restart chain", edit_related)
            form.addRow("Selected", self.related_summary)
        note = QLabel(
            "Safety safeguard: automatic actions are rate-limited and audited. Disabling monitoring from the target list suppresses checks, actions, and alerts without deleting this configuration."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#98a6aa")
        form.addRow("", note)
        return page

    def update_related_summary(self) -> None:
        count = len(self.related_selected)
        preview = ", ".join(sorted(self.related_selected)[:3])
        suffix = f" +{count - 3} more" if count > 3 else ""
        self.related_summary.setText(
            f"{count} selected · {preview}{suffix}" if count else "No related services selected"
        )

    def edit_related_services(self) -> None:
        dialog = RelatedServicesDialog(self.key, self.related_selected, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.related_selected = dialog.selected_services
            self.update_related_summary()

    def _api_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(22, 24, 22, 24)
        form.setSpacing(14)
        api = self.config.get("api_check", {})
        self.api_enabled = QCheckBox("Monitor a linked HTTP/HTTPS endpoint")
        self.api_enabled.setChecked(api.get("enabled", False))
        self.api_url = QLineEdit(api.get("url", ""))
        self.api_url.setPlaceholderText("https://server.example.com/health")
        self.api_status = QSpinBox()
        self.api_status.setRange(100, 599)
        self.api_status.setValue(int(api.get("expected_status", 200)))
        self.api_text = QLineEdit(api.get("expected_text", ""))
        self.api_text.setPlaceholderText("Optional text expected in the response")
        self.api_timeout = QSpinBox()
        self.api_timeout.setRange(1, 60)
        self.api_timeout.setValue(int(api.get("timeout_seconds", 10)))
        self.api_timeout.setSuffix(" seconds")
        self.api_failures = QSpinBox()
        self.api_failures.setRange(1, 20)
        self.api_failures.setValue(int(api.get("action_after_failures", 3)))
        self.api_action = QComboBox()
        self.api_action.addItem("Notify only", "notify")
        if self.kind == "service":
            self.api_action.addItem("Restart service", "restart")
        else:
            self.api_action.addItem("Recycle IIS pool", "recycle")
            self.api_action.addItem("Start IIS pool", "start")
        action_index = self.api_action.findData(api.get("failure_action", "notify"))
        self.api_action.setCurrentIndex(max(0, action_index))
        form.addRow("API check", self.api_enabled)
        form.addRow("Endpoint URL", self.api_url)
        form.addRow("Expected status", self.api_status)
        form.addRow("Expected response text", self.api_text)
        form.addRow("Request timeout", self.api_timeout)
        form.addRow("Failures before escalation", self.api_failures)
        form.addRow("After repeated failure", self.api_action)
        return page

    def _schedule_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(22, 24, 22, 24)
        form.setSpacing(14)
        schedule = self.config.get("schedule", {})
        self.schedule_enabled = QCheckBox("Enable a scheduled restart/recycle")
        self.schedule_enabled.setChecked(schedule.get("enabled", False))
        self.schedule_frequency = QComboBox()
        self.schedule_frequency.addItems(["Daily", "Weekly", "Monthly", "Interval"])
        self.schedule_frequency.setCurrentText(schedule.get("frequency", "Daily"))
        self.schedule_action = QComboBox()
        self.schedule_action.addItems(
            ["Restart"] if self.kind == "service" else ["Recycle", "Restart"]
        )
        self.schedule_action.setCurrentText(schedule.get("action", "restart").title())
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        stored_time = QTime.fromString(schedule.get("time", "02:00"), "HH:mm")
        self.schedule_time.setTime(stored_time if stored_time.isValid() else QTime(2, 0))
        self.schedule_weekday = QComboBox()
        self.schedule_weekday.addItems(
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        )
        self.schedule_weekday.setCurrentIndex(int(schedule.get("weekday", 0)))
        self.schedule_month_day = QSpinBox()
        self.schedule_month_day.setRange(1, 31)
        self.schedule_month_day.setValue(int(schedule.get("month_day", 1)))
        self.schedule_month_day.setToolTip(
            "For shorter months, Guardian safely uses that month's final day."
        )
        self.schedule_interval = QSpinBox()
        self.schedule_interval.setRange(5, 10080)
        self.schedule_interval.setValue(int(schedule.get("interval_minutes", 60)))
        self.schedule_interval.setSuffix(" minutes")
        self.schedule_grace = QSpinBox()
        self.schedule_grace.setRange(1, 1440)
        self.schedule_grace.setValue(int(schedule.get("missed_run_minutes", 10)))
        self.schedule_grace.setSuffix(" minutes")
        self.schedule_grace.setToolTip(
            "If the agent was offline, run only when it returns within this safe grace period."
        )
        form.addRow("Scheduled action", self.schedule_enabled)
        form.addRow("Action", self.schedule_action)
        form.addRow("Frequency", self.schedule_frequency)
        form.addRow("Execution time", self.schedule_time)
        form.addRow("Weekly day", self.schedule_weekday)
        form.addRow("Monthly day", self.schedule_month_day)
        form.addRow("Interval", self.schedule_interval)
        form.addRow("Missed-run grace", self.schedule_grace)
        note = QLabel(
            "Every occurrence receives a unique action claim. Repeated checks, service restarts, or concurrent agents cannot execute it twice."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#98a6aa")
        form.addRow("Safety", note)
        return page

    def _maintenance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(22, 24, 22, 24)
        form.setSpacing(14)
        maintenance = self.config.get("maintenance", {})
        self.maintenance_enabled = QCheckBox("Enable a daily planned downtime window")
        self.maintenance_enabled.setChecked(maintenance.get("enabled", False))
        self.maintenance_start = QTimeEdit()
        self.maintenance_start.setDisplayFormat("HH:mm")
        maintenance_start = QTime.fromString(maintenance.get("start", "00:00"), "HH:mm")
        self.maintenance_start.setTime(
            maintenance_start if maintenance_start.isValid() else QTime(0, 0)
        )
        self.maintenance_end = QTimeEdit()
        self.maintenance_end.setDisplayFormat("HH:mm")
        maintenance_end = QTime.fromString(maintenance.get("end", "00:30"), "HH:mm")
        self.maintenance_end.setTime(maintenance_end if maintenance_end.isValid() else QTime(0, 30))
        self.maintenance_suppress = QCheckBox("Suppress health checks, recovery, and alerts")
        self.maintenance_suppress.setChecked(maintenance.get("suppress_monitoring", True))
        self.maintenance_stop_start = QCheckBox("Stop at the beginning and start at the end")
        self.maintenance_stop_start.setChecked(maintenance.get("stop_start", False))
        self.maintenance_resume_grace = QSpinBox()
        self.maintenance_resume_grace.setRange(1, 1440)
        self.maintenance_resume_grace.setValue(int(maintenance.get("resume_grace_minutes", 30)))
        self.maintenance_resume_grace.setSuffix(" minutes")
        form.addRow("Maintenance window", self.maintenance_enabled)
        form.addRow("Starts at", self.maintenance_start)
        form.addRow("Ends at", self.maintenance_end)
        form.addRow("During the window", self.maintenance_suppress)
        form.addRow("Boundary actions", self.maintenance_stop_start)
        form.addRow("Resume grace", self.maintenance_resume_grace)
        note = QLabel(
            "Overnight windows are supported automatically. Planned stops are not reported as incidents."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#98a6aa")
        form.addRow("Hint", note)
        return page

    def save(self) -> None:
        url = self.api_url.text().strip()
        if self.api_enabled.isChecked() and not url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self,
                "Invalid API endpoint",
                "Enter a complete HTTP or HTTPS URL before enabling the API check.",
            )
            return
        self.config.update(
            {
                "start_if_stopped": self.start_if_stopped.isChecked(),
                # Retained for compatibility with earlier 0.1.0 configurations.
                "auto_restart": self.start_if_stopped.isChecked(),
                "retry_count": self.retry_count.value(),
                "retry_delay_seconds": self.retry_delay.value(),
            }
        )
        if self.kind == "service":
            self.config["related_services"] = sorted(self.related_selected)
        self.config["api_check"] = {
            "enabled": self.api_enabled.isChecked(),
            "url": url,
            "expected_status": self.api_status.value(),
            "expected_text": self.api_text.text(),
            "timeout_seconds": self.api_timeout.value(),
            "action_after_failures": self.api_failures.value(),
            "failure_action": self.api_action.currentData(),
        }
        self.config["schedule"] = {
            "enabled": self.schedule_enabled.isChecked(),
            "action": self.schedule_action.currentText().lower(),
            "frequency": self.schedule_frequency.currentText(),
            "time": self.schedule_time.time().toString("HH:mm"),
            "weekday": self.schedule_weekday.currentIndex(),
            "month_day": self.schedule_month_day.value(),
            "interval_minutes": self.schedule_interval.value(),
            "missed_run_minutes": self.schedule_grace.value(),
        }
        self.config["maintenance"] = {
            "enabled": self.maintenance_enabled.isChecked(),
            "start": self.maintenance_start.time().toString("HH:mm"),
            "end": self.maintenance_end.time().toString("HH:mm"),
            "weekdays": list(range(7)),
            "suppress_monitoring": self.maintenance_suppress.isChecked(),
            "stop_start": self.maintenance_stop_start.isChecked(),
            "resume_grace_minutes": self.maintenance_resume_grace.value(),
        }
        self.store.set_target_config(self.kind, self.key, self.config)
        self.store.add_event(
            "info",
            "configuration",
            self.key,
            f"{self.kind.title()} monitoring configuration updated",
        )
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.setWindowTitle(f"MiMonitoringService v{__version__}")
        icon = resource_path("assets/mimonitoringservice-icon.png")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1280, 800)
        self.setMinimumSize(980, 640)
        root = QWidget()
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(205)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(12, 18, 12, 14)
        brand = QLabel(f"MILLENSYS\nMiMonitoringService\nv{__version__}")
        brand.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        brand.setStyleSheet("color:#f2f7f8; border-left:3px solid #35c7d4; padding-left:9px")
        nav.addWidget(brand)
        nav.addSpacing(28)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.nav_buttons: dict[str, QPushButton] = {}
        for label, factory in (
            ("Overview", self.dashboard_page),
            ("Windows Services", lambda: self.targets_page("service")),
            ("IIS Pools", lambda: self.targets_page("pool")),
            ("Disks", self.disk_page),
            ("Performance", self.performance_page),
            ("SQL Operations", self.sql_operations_page),
            ("Assurance", self.assurance_page),
            ("Reports", self.reports_page),
            ("Events & Logs", self.events_page),
            ("License", self.license_page),
            ("Configuration", self.settings_page),
        ):
            page = factory()
            self.pages[label] = page
            self.stack.addWidget(page)
            button = QPushButton(label)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, name=label: self.show_page(name))
            self.nav_buttons[label] = button
            nav.addWidget(button)
        self.nav_buttons["Overview"].setChecked(True)
        nav.addStretch()
        guide_button = QPushButton("User Guide")
        guide_button.clicked.connect(lambda: open_user_guide(self))
        about_button = QPushButton("About")
        about_button.clicked.connect(self.show_about)
        nav.addWidget(guide_button)
        nav.addWidget(about_button)
        copyright_label = QLabel("© 2026 Millensys Support Department\nEng. Metwally Hammam")
        copyright_label.setToolTip(COPYRIGHT)
        copyright_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        copyright_label.setStyleSheet("color:#78858a;font-size:8pt")
        nav.addWidget(copyright_label)
        self.agent_console_status = QLabel("● Checking monitoring agent…")
        self.agent_console_status.setWordWrap(True)
        self.agent_console_status.setStyleSheet("color:#f2b84b")
        nav.addWidget(self.agent_console_status)
        shell.addWidget(sidebar)
        shell.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.allow_close = False
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        if self.tray_available:
            self.setup_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_visible)
        self.timer.start(10_000)
        self.refresh_visible()

    def setup_tray(self) -> None:
        menu = QMenu()
        open_action = QAction("Open MiMonitoringService", self)
        start_action = QAction("Start monitoring service", self)
        stop_action = QAction("Stop monitoring service", self)
        exit_action = QAction("Exit console", self)
        open_action.triggered.connect(self.show_from_tray)
        start_action.triggered.connect(lambda: self.tray_service_action("start"))
        stop_action.triggered.connect(lambda: self.tray_service_action("stop"))
        exit_action.triggered.connect(self.exit_console)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(start_action)
        menu.addAction(stop_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.setToolTip("MiMonitoringService")
        self.tray_icon.activated.connect(
            lambda reason: self.show_from_tray()
            if reason
            in {
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick,
            }
            else None
        )
        self.tray_icon.show()
        QApplication.instance().setQuitOnLastWindowClosed(False)

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_service_action(self, action: str) -> None:
        if action == "start":
            license_status = current_license()
            if not license_status.valid:
                self.tray_icon.showMessage(
                    "Activation required",
                    license_status.message,
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
                return
        ok, message = manage_agent_service(action)
        self.tray_icon.showMessage(
            "MiMonitoringService",
            message or ("Completed" if ok else "Action failed"),
            QSystemTrayIcon.MessageIcon.Information
            if ok
            else QSystemTrayIcon.MessageIcon.Critical,
            4000,
        )
        self.refresh_agent_status()

    def exit_console(self) -> None:
        self.allow_close = True
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        close_to_tray = self.store.get("configuration")["agent"].get(
            "close_to_tray", True
        )
        if not self.allow_close and self.tray_available and close_to_tray:
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "MiMonitoringService is still running",
                "The console was hidden to the system tray. Monitoring continues in the Windows service.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            return
        event.accept()

    def show_about(self) -> None:
        identity = server_identity()
        license_status = current_license()
        license_text = license_status.message
        QMessageBox.about(
            self,
            "About MiMonitoringService",
            f"<h2>MiMonitoringService</h2>"
            f"<p>MiMonitoringService v{__version__}</p>"
            f"<p><b>Server:</b> {identity.server_name}<br>"
            f"<b>MAC address:</b> {identity.mac_address}</p>"
            f"<p><b>License:</b> {license_text}</p>"
            f"<p>{COPYRIGHT}</p>",
        )

    def page_shell(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setFont(QFont("Segoe UI", 19, QFont.Weight.Bold))
        layout.addWidget(heading)
        detail = QLabel(subtitle)
        detail.setStyleSheet("color:#98a6aa")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        return page, layout

    def dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Infrastructure health dashboard")
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        self.dashboard_identity = QLabel("Reading server identity…")
        self.dashboard_identity.setStyleSheet("color:#98a6aa")
        title_box.addWidget(title)
        title_box.addWidget(self.dashboard_identity)
        self.dashboard_agent = QLabel("● Checking monitoring service…")
        self.dashboard_agent.setAlignment(Qt.AlignmentFlag.AlignRight)
        service_controls = QPushButton("Service controls")
        service_controls.clicked.connect(lambda: self.show_page("Configuration"))
        incidents = QPushButton("Incidents")
        incidents.setObjectName("primary")
        incidents.clicked.connect(self.open_score_incidents)
        changes = QPushButton("Changes")
        changes.clicked.connect(self.open_component_changes)
        header.addLayout(title_box)
        header.addStretch()
        header.addWidget(self.dashboard_agent)
        header.addWidget(incidents)
        header.addWidget(changes)
        header.addWidget(service_controls)
        layout.addLayout(header)

        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(225)
        hero.setMaximumHeight(255)
        hero_grid = QGridLayout(hero)
        hero_grid.setContentsMargins(18, 14, 18, 14)
        eyebrow = QLabel("SERVER HEALTH")
        eyebrow.setObjectName("eyebrow")
        self.health_gauge = HealthGauge()
        self.health_status_value = QLabel("Calculating…")
        self.health_status_value.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        self.health_explanation = QLabel("Reading incidents and monitoring coverage…")
        self.health_explanation.setWordWrap(True)
        self.health_explanation.setStyleSheet("color:#b4bec1")
        hero_grid.addWidget(eyebrow, 0, 0)
        hero_grid.addWidget(self.health_gauge, 1, 0, 2, 1)
        hero_grid.addWidget(self.health_status_value, 1, 1)
        hero_grid.addWidget(self.health_explanation, 2, 1)
        hero_grid.setColumnStretch(1, 1)
        cards = QGridLayout()
        cards.setContentsMargins(0, 4, 0, 0)
        cards.setHorizontalSpacing(12)
        self.cpu_card = self.card("CPU", "—")
        self.memory_card = self.card("Memory", "—")
        self.disk_card = self.card("Lowest disk free", "—")
        self.alert_card = self.card("Active incidents", "—")
        self.service_card = self.card("Services", "—")
        self.pool_card = self.card("IIS pools", "—")
        for index, card in enumerate(
            (self.cpu_card, self.memory_card, self.disk_card, self.service_card, self.pool_card, self.alert_card)
        ):
            card.setObjectName("metricCard")
            card.setMinimumHeight(78)
            card.findChild(QLabel, "value").setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
            cards.addWidget(card, 0, index)
            cards.setColumnStretch(index, 1)
        hero_grid.addLayout(cards, 3, 0, 1, 2)
        layout.addWidget(hero)

        panels = QGridLayout()
        issue_card = QFrame()
        issue_card.setObjectName("card")
        issue_box = QVBoxLayout(issue_card)
        issue_title = QLabel("ACTIVE INCIDENTS · WHAT NEEDS ATTENTION")
        issue_title.setObjectName("eyebrow")
        issue_box.addWidget(issue_title)
        self.dashboard_issues = QListWidget()
        self.dashboard_issues.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.dashboard_issues.setToolTip("Double-click an item to inspect its incident history")
        self.dashboard_issues.itemDoubleClicked.connect(self.open_issue_item)
        issue_box.addWidget(self.dashboard_issues)
        coverage_card = QFrame()
        coverage_card.setObjectName("card")
        coverage_box = QVBoxLayout(coverage_card)
        coverage_title = QLabel("MONITORING COVERAGE")
        coverage_title.setObjectName("eyebrow")
        coverage_box.addWidget(coverage_title)
        self.service_coverage = self.coverage_row(coverage_box, "Windows Services")
        self.pool_coverage = self.coverage_row(coverage_box, "IIS Application Pools")
        self.disk_coverage = self.coverage_row(coverage_box, "Disks")
        self.api_coverage = self.coverage_row(coverage_box, "API checks")
        panels.addWidget(issue_card, 0, 0)
        panels.addWidget(coverage_card, 0, 1)
        panels.setColumnStretch(0, 3)
        panels.setColumnStretch(1, 2)
        layout.addLayout(panels, 1)

        actions_card = QFrame()
        actions_card.setObjectName("card")
        actions_box = QVBoxLayout(actions_card)
        actions_title = QLabel("AUTOMATIC ACTIONS & RECOVERIES")
        actions_title.setObjectName("eyebrow")
        actions_box.addWidget(actions_title)
        self.dashboard_actions = QListWidget()
        self.dashboard_actions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        actions_box.addWidget(self.dashboard_actions)
        layout.addWidget(actions_card, 1)
        return page

    def open_score_incidents(self) -> None:
        ScoreIncidentsDialog(self.store, self).exec()
        self.refresh_dashboard()

    def card(self, caption: str, value: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("card")
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 9, 12, 9)
        box.setSpacing(2)
        label = QLabel(caption)
        label.setStyleSheet("color:#98a6aa")
        number = QLabel(value)
        number.setObjectName("value")
        number.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        progress = QProgressBar()
        progress.setObjectName("progress")
        progress.setRange(0, 100)
        progress.hide()
        box.addWidget(label)
        box.addWidget(number)
        box.addWidget(progress)
        return frame

    def assurance_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Component assurance",
            "File integrity monitoring and approved Windows Service and IIS component baselines.",
        )
        tabs = QTabWidget()
        backup_page = QWidget()
        backup_layout = QVBoxLayout(backup_page)
        backup_note = QLabel(
            "Lightweight backup-file assurance. Select a folder or file, set the expected age, "
            "and MiMonitoringService checks the evidence without connecting to SQL Server."
        )
        backup_note.setWordWrap(True); backup_note.setStyleSheet("color:#98a6aa")
        backup_layout.addWidget(backup_note)
        backup_actions = QHBoxLayout()
        add_backup = QPushButton("+ Add backup check"); add_backup.setObjectName("primary")
        edit_backup = QPushButton("Edit selected")
        remove_backup = QPushButton("Remove selected")
        check_backup = QPushButton("Check selected now")
        acknowledge_backup = QPushButton("Acknowledge selected")
        add_backup.clicked.connect(lambda: self.edit_backup_rule())
        edit_backup.clicked.connect(lambda: self.edit_backup_rule(self.backup_table.currentRow()))
        remove_backup.clicked.connect(self.remove_backup_rule)
        check_backup.clicked.connect(self.check_backup_rule_now)
        acknowledge_backup.clicked.connect(self.acknowledge_backup_rule)
        for button in (add_backup, edit_backup, remove_backup, check_backup, acknowledge_backup): backup_actions.addWidget(button)
        backup_actions.addStretch(); backup_layout.addLayout(backup_actions)
        self.backup_table = self.table(["Name", "Folder or file", "Pattern", "Daily check", "Maximum age", "Latest evidence", "Status", "Acknowledged", "Enabled"])
        self.backup_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.backup_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        backup_layout.addWidget(self.backup_table)

        file_page = QWidget()
        file_layout = QVBoxLayout(file_page)
        file_actions = QHBoxLayout()
        add_file = QPushButton("+ Add file monitor")
        add_file.setObjectName("primary")
        edit_file = QPushButton("Edit selected")
        remove_file = QPushButton("Remove selected")
        add_file.clicked.connect(lambda: self.edit_file_rule())
        edit_file.clicked.connect(lambda: self.edit_file_rule(self.file_table.currentRow()))
        remove_file.clicked.connect(self.remove_file_rule)
        for button in (add_file, edit_file, remove_file):
            file_actions.addWidget(button)
        file_actions.addStretch()
        file_layout.addLayout(file_actions)
        self.file_table = self.table(["Name", "Specific file", "Enabled"])
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        file_layout.addWidget(self.file_table)

        changes_page = QWidget()
        changes_layout = QVBoxLayout(changes_page)
        changes_note = QLabel(
            "Windows Services and IIS pools are snapshotted automatically when monitored. "
            "Open Changes to compare old and new values or accept a new approved baseline."
        )
        changes_note.setWordWrap(True)
        changes_button = QPushButton("Open Changes…")
        changes_button.setObjectName("primary")
        changes_button.clicked.connect(self.open_component_changes)
        changes_layout.addWidget(changes_note)
        changes_layout.addWidget(changes_button)
        changes_layout.addStretch()
        tabs.addTab(backup_page, "Backup Files")
        tabs.addTab(file_page, "File Integrity")
        tabs.addTab(changes_page, "Component Changes")
        layout.addWidget(tabs, 1)
        self.refresh_assurance_rules()
        return page

    def sql_configuration_page(self) -> QWidget:
        page, layout = self.page_shell(
            "SQL monitoring configuration",
            "Configure SQL connections, native backup compliance, performance evidence, and monitored Agent jobs.",
        )
        tabs = QTabWidget()
        sql_page = QWidget(); sql_layout = QVBoxLayout(sql_page); sql_actions = QHBoxLayout()
        add_sql = QPushButton("+ Add SQL Server"); add_sql.setObjectName("primary")
        edit_sql = QPushButton("Edit selected"); remove_sql = QPushButton("Remove selected")
        add_sql.clicked.connect(lambda: self.edit_sql_monitor())
        edit_sql.clicked.connect(lambda: self.edit_sql_monitor(self.sql_table.currentRow()))
        remove_sql.clicked.connect(self.remove_sql_monitor)
        for button in (add_sql, edit_sql, remove_sql): sql_actions.addWidget(button)
        sql_actions.addStretch(); sql_layout.addLayout(sql_actions)
        self.sql_table = self.table(["Name", "Server", "Database", "Connection", "Selected jobs", "Latest job results", "Enabled"])
        self.sql_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection); sql_layout.addWidget(self.sql_table)
        tabs.addTab(sql_page, "SQL Servers & Agent Jobs")
        layout.addWidget(tabs, 1)
        return page

    def sql_operations_page(self) -> QWidget:
        page, layout = self.page_shell(
            "SQL Operations",
            "One workspace for database capacity, backup recovery, SQL Agent jobs, and monitoring configuration.",
        )
        self.sql_operations_tabs = QTabWidget()
        self.sql_operations_tabs.setDocumentMode(True)
        self.sql_operations_tabs.addTab(self.databases_page(), "Databases")
        self.sql_operations_tabs.addTab(self.jobs_page(), "Agent Jobs")
        self.sql_operations_tabs.addTab(self.sql_configuration_page(), "Configuration")
        self.sql_operations_tabs.currentChanged.connect(self.refresh_sql_operations)
        layout.addWidget(self.sql_operations_tabs, 1)
        return page

    def show_sql_operations(self, tab_index: int = 0) -> None:
        self.show_page("SQL Operations")
        self.sql_operations_tabs.setCurrentIndex(max(0, min(tab_index, 2)))

    def refresh_sql_operations(self, tab_index: int = -1) -> None:
        index = self.sql_operations_tabs.currentIndex() if tab_index < 0 else tab_index
        if index == 0:
            self.refresh_databases()
        elif index == 1:
            self.refresh_jobs()
        else:
            self.refresh_assurance_rules()

    def databases_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Database overview",
            "SQL Server connectivity, file utilization, autogrowth configuration, volume headroom, and current capacity risks.",
        )
        toolbar = QHBoxLayout()
        self.database_profile_filter = QComboBox()
        self.database_profile_filter.currentIndexChanged.connect(self.refresh_databases)
        refresh = QPushButton("Refresh view")
        refresh.clicked.connect(self.refresh_databases)
        configure = QPushButton("Configure SQL monitoring")
        configure.setObjectName("primary")
        configure.clicked.connect(lambda: self.show_sql_operations(2))
        toolbar.addWidget(QLabel("SQL instance"))
        toolbar.addWidget(self.database_profile_filter)
        toolbar.addWidget(refresh)
        toolbar.addStretch()
        toolbar.addWidget(configure)
        layout.addLayout(toolbar)

        database_tabs = QTabWidget()
        capacity_page = QWidget(); capacity_layout = QVBoxLayout(capacity_page)
        backup_page = QWidget(); backup_layout = QVBoxLayout(backup_page)
        database_tabs.addTab(capacity_page, "Capacity & storage")
        database_tabs.addTab(backup_page, "Backup & recovery")
        layout.addWidget(database_tabs, 1)

        cards = QGridLayout()
        self.database_connection_card = self.card("Connection", "Not configured")
        self.database_capacity_card = self.card("Capacity health", "No data")
        self.database_file_card = self.card("Database files", "0")
        self.database_risk_card = self.card("Capacity risks", "0")
        for index, card in enumerate(
            (
                self.database_connection_card,
                self.database_capacity_card,
                self.database_file_card,
                self.database_risk_card,
            )
        ):
            cards.addWidget(card, 0, index)
        capacity_layout.addLayout(cards)

        self.database_freshness = QLabel(
            "No capacity sample yet. Capacity collection runs independently at a safe interval."
        )
        self.database_freshness.setWordWrap(True)
        self.database_freshness.setStyleSheet("color:#98a6aa")
        capacity_layout.addWidget(self.database_freshness)
        self.database_capacity_trend = CapacityTrendWidget()
        capacity_layout.addWidget(self.database_capacity_trend)
        self.database_file_table = self.table(
            [
                "Database / file",
                "Type",
                "Allocated",
                "Used",
                "Free",
                "Utilization",
                "Autogrowth",
                "Maximum",
                "Volume",
                "Volume free",
                "Forecast",
                "Health interpretation",
            ]
        )
        self.database_file_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.database_file_table.horizontalHeader().setSectionResizeMode(
            11, QHeaderView.ResizeMode.Stretch
        )
        capacity_layout.addWidget(self.database_file_table, 1)
        backup_title = QLabel("Backup and recovery assurance")
        backup_title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        backup_layout.addWidget(backup_title)
        backup_cards = QGridLayout()
        self.database_backup_sla_card = self.card("Backup SLA", "No data")
        self.database_rpo_card = self.card("Recovery-point exposure", "—")
        self.database_chain_card = self.card("Log chain", "No data")
        self.database_history_card = self.card("Evidence records", "0")
        self.database_backup_anomaly_card = self.card("Baseline anomalies", "0")
        self.database_readiness_card = self.card("Recovery readiness", "No evidence")
        for index, card in enumerate((self.database_backup_sla_card, self.database_rpo_card, self.database_chain_card, self.database_history_card, self.database_backup_anomaly_card, self.database_readiness_card)):
            backup_cards.addWidget(card, 0, index)
        backup_layout.addLayout(backup_cards)
        self.database_backup_freshness = QLabel("No SQL-native backup evidence collected yet.")
        self.database_backup_freshness.setWordWrap(True); self.database_backup_freshness.setStyleSheet("color:#98a6aa")
        backup_layout.addWidget(self.database_backup_freshness)
        self.database_backup_calendar = BackupCalendarWidget()
        backup_layout.addWidget(self.database_backup_calendar)
        self.database_chain_timeline = BackupChainTimelineWidget(); backup_layout.addWidget(self.database_chain_timeline)
        self.database_backup_table = self.table(["Backup type", "Required", "SLA status", "Latest finish", "Age", "Maximum", "Destination", "Evidence"])
        self.database_backup_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        backup_layout.addWidget(self.database_backup_table, 1)
        evidence_header = QHBoxLayout()
        evidence_title = QLabel("Verification and restore-test evidence"); evidence_title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        record_evidence = QPushButton("Record evidence…"); record_evidence.setObjectName("primary"); record_evidence.clicked.connect(self.record_recovery_evidence)
        evidence_header.addWidget(evidence_title); evidence_header.addStretch(); evidence_header.addWidget(record_evidence); backup_layout.addLayout(evidence_header)
        self.database_recovery_table = self.table(["Performed", "Database", "Evidence type", "Result", "Owner", "Destination", "Duration", "Auditable reference"])
        self.database_recovery_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        backup_layout.addWidget(self.database_recovery_table, 1)
        return page

    def jobs_page(self) -> QWidget:
        page, layout = self.page_shell(
            "SQL Agent job intelligence",
            "Execution outcomes, success percentage, duration baselines, failure streaks, and overdue jobs.",
        )
        toolbar = QHBoxLayout(); self.job_profile_filter = QComboBox()
        self.job_profile_filter.currentIndexChanged.connect(self.refresh_jobs)
        refresh = QPushButton("Refresh view"); refresh.clicked.connect(self.refresh_jobs)
        run_job = QPushButton("Run selected job…"); run_job.clicked.connect(self.run_selected_job)
        configure = QPushButton("Configure monitored jobs"); configure.setObjectName("primary"); configure.clicked.connect(lambda: self.show_sql_operations(2))
        toolbar.addWidget(QLabel("SQL instance")); toolbar.addWidget(self.job_profile_filter); toolbar.addWidget(refresh); toolbar.addWidget(run_job); toolbar.addStretch(); toolbar.addWidget(configure); layout.addLayout(toolbar)
        cards = QGridLayout(); self.jobs_total_card = self.card("Monitored jobs", "0")
        self.jobs_success_card = self.card("Average success", "—"); self.jobs_failure_card = self.card("Failure streaks", "0"); self.jobs_overdue_card = self.card("Overdue jobs", "0")
        for index, card in enumerate((self.jobs_total_card, self.jobs_success_card, self.jobs_failure_card, self.jobs_overdue_card)): cards.addWidget(card, 0, index)
        layout.addLayout(cards)
        self.jobs_freshness = QLabel("Job intelligence appears after completed SQL Agent executions are collected."); self.jobs_freshness.setWordWrap(True); self.jobs_freshness.setStyleSheet("color:#98a6aa"); layout.addWidget(self.jobs_freshness)
        self.jobs_duration_trend = JobDurationTrendWidget(); layout.addWidget(self.jobs_duration_trend)
        self.jobs_intelligence_table = self.table(["Instance / job", "Latest status", "Last run", "Next run", "Success", "Median", "P95", "Failure streak", "Schedule health", "Duration health", "Latest failed step"])
        self.jobs_intelligence_table.currentCellChanged.connect(self.refresh_job_duration_trend)
        self.jobs_intelligence_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.jobs_intelligence_table, 1)
        return page

    def refresh_jobs(self) -> None:
        if not hasattr(self, "job_profile_filter"): return
        profiles = [item for item in self.store.get("configuration").get("sql_monitors", []) if item.get("enabled", True)]
        selected = self.job_profile_filter.currentData(); self.job_profile_filter.blockSignals(True); self.job_profile_filter.clear(); self.job_profile_filter.addItem("All configured SQL instances", "")
        for profile in profiles: self.job_profile_filter.addItem(str(profile.get("name") or profile.get("server")), str(profile.get("id") or profile.get("server")))
        index = self.job_profile_filter.findData(selected); self.job_profile_filter.setCurrentIndex(max(0, index)); self.job_profile_filter.blockSignals(False)
        selected = str(self.job_profile_filter.currentData() or ""); runtime = self.store.get("sql_runtime", {}); rows = []
        for profile in profiles:
            profile_id = str(profile.get("id") or profile.get("server"))
            if selected and selected != profile_id: continue
            for name in profile.get("jobs", []): rows.append((profile, name, runtime.get(profile_id, {}).get("jobs", {}).get(name, {})))
        intelligence = [job.get("intelligence", {}) for _profile, _name, job in rows]
        success = [float(item["success_percent"]) for item in intelligence if item.get("success_percent") is not None]
        failures = sum(1 for item in intelligence if int(item.get("failure_streak", 0)) > 0); overdue = sum(1 for item in intelligence if item.get("overdue"))
        self.jobs_total_card.findChild(QLabel, "value").setText(str(len(rows))); self.jobs_success_card.findChild(QLabel, "value").setText("—" if not success else f"{sum(success)/len(success):.1f}%")
        self.jobs_failure_card.findChild(QLabel, "value").setText(str(failures)); self.jobs_overdue_card.findChild(QLabel, "value").setText(str(overdue))
        self.jobs_freshness.setText("Persistent history survives service restarts. Percentiles become more reliable as executions accumulate.")
        self.jobs_intelligence_table.setRowCount(len(rows))
        self._job_view_rows = rows
        for row, (profile, name, job) in enumerate(rows):
            info = job.get("intelligence", {}); median_value = info.get("median_seconds"); p95 = info.get("p95_seconds")
            schedule_health = str(job.get("schedule", {}).get("status") or ("Overdue" if info.get("overdue") else "On schedule"))
            failed_step = job.get("latest_failed_step", {})
            failure_text = "—" if not failed_step else f"{failed_step.get('step_name')}: {failed_step.get('message', '')}"
            duration_health = "Long running" if job.get("long_running") else "Abnormal" if info.get("abnormal_duration") else "Normal"
            values = (f"{profile.get('name') or profile.get('server')} / {name}", job.get("status", "Not checked"), job.get("last_run", "—"), job.get("next_run", "—"), "—" if info.get("success_percent") is None else f"{info['success_percent']:.1f}%", "—" if median_value is None else f"{median_value/60:.1f} min", "—" if p95 is None else f"{p95/60:.1f} min", str(info.get("failure_streak", 0)), schedule_health, duration_health, failure_text)
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if (column == 1 and value not in ("Succeeded", "Not checked", "Running")) or (column == 8 and value != "On schedule") or (column == 9 and value in ("Abnormal", "Long running")): item.setForeground(QColor("#ffb4a2"))
                self.jobs_intelligence_table.setItem(row, column, item)
        if rows:
            self.jobs_intelligence_table.selectRow(0); self.refresh_job_duration_trend(0)
        else:
            self.jobs_duration_trend.set_history([])

    def refresh_job_duration_trend(self, row: int, *_args) -> None:
        if row < 0 or row >= len(getattr(self, "_job_view_rows", [])):
            self.jobs_duration_trend.set_history([]); return
        profile, name, job = self._job_view_rows[row]
        profile_id = str(profile.get("id") or profile.get("server"))
        self.jobs_duration_trend.set_history([dict(item) for item in self.store.sql_job_history(profile_id, name)], str(job.get("next_run", "")))

    def run_selected_job(self) -> None:
        row = self.jobs_intelligence_table.currentRow()
        if row < 0 or row >= len(getattr(self, "_job_view_rows", [])):
            QMessageBox.information(self, "Select job", "Select a monitored SQL Agent job first."); return
        profile, name, job = self._job_view_rows[row]
        if not profile.get("allow_manual_job_run", False):
            QMessageBox.warning(self, "Manual execution disabled", "Enable confirmed manual execution in the SQL monitoring profile first."); return
        if job.get("status") == "Running":
            QMessageBox.warning(self, "Job already running", "This SQL Agent job is already running."); return
        answer = QMessageBox.question(self, "Confirm SQL Agent execution", f"Start SQL Agent job '{name}' on {profile.get('server')}?\n\nThis action is audited.")
        if answer != QMessageBox.StandardButton.Yes: return
        try:
            start_agent_job(profile, name)
        except Exception as exc:  # noqa: BLE001 - show safe provider error and audit failure
            self.store.add_event("error", "sql-job-action", name, f"Manual SQL Agent start failed: {exc}", server=profile.get("server"))
            QMessageBox.critical(self, "Job start failed", str(exc)); return
        self.store.add_event("info", "sql-job-action", name, "Manual SQL Agent start requested by console operator", server=profile.get("server"))
        QMessageBox.information(self, "Job start requested", f"SQL Agent accepted the start request for '{name}'.")
        self.refresh_jobs()

    def kill_selected_sql_sessions(self) -> None:
        selected_rows = self.sql_sessions_table.selectionModel().selectedRows()
        session_ids = sorted({int(item.data(Qt.ItemDataRole.UserRole)) for index in selected_rows if (item := self.sql_sessions_table.item(index.row(), 0)) is not None and item.data(Qt.ItemDataRole.UserRole) is not None})
        if not session_ids:
            QMessageBox.information(self, "Select SQL sessions", "Select one or more session rows first.")
            return
        profile_id = str(self.sql_performance_profile.currentData() or "")
        profile = next((item for item in self.store.get("configuration").get("sql_monitors", []) if str(item.get("id") or item.get("server")) == profile_id), None)
        if not profile:
            QMessageBox.warning(self, "SQL profile unavailable", "The selected SQL monitoring profile no longer exists.")
            return
        spids = ", ".join(str(value) for value in session_ids)
        answer = QMessageBox.question(self, "Confirm SQL session termination", f"Terminate {len(session_ids)} selected SQL session(s) on {profile.get('server')}?\n\nSPIDs: {spids}\n\nSQL Server may roll back open transactions, which can take time. Sessions are revalidated before termination; system and monitoring sessions are protected. Every result is written to the audit trail.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not profile.get("allow_session_termination", False):
            QMessageBox.warning(self, "Session termination disabled", "Enable confirmed session termination in the SQL monitoring profile first.")
            return
        current_sessions = self.store.get("sql_runtime", {}).get(profile_id, {}).get("sessions", {}).get("sessions", [])
        expected_sessions = {int(item["session_id"]): item for item in current_sessions if int(item.get("session_id", 0)) in session_ids}
        try:
            results = kill_sessions(profile, session_ids, expected_sessions=expected_sessions)
        except Exception as exc:  # noqa: BLE001 - report and audit the provider failure
            self.store.add_event("error", "sql-session-action", str(profile.get("name") or profile.get("server")), f"SQL session termination request failed: {exc}", server=profile.get("server"), session_ids=session_ids)
            QMessageBox.critical(self, "Session termination failed", str(exc))
            return
        succeeded = 0
        messages = []
        for result in results:
            ok = bool(result.get("ok")); succeeded += int(ok)
            message = str(result.get("message") or "No provider response")
            session_id = int(result.get("session_id", 0))
            self.store.add_event("info" if ok else "error", "sql-session-action", f"SPID {session_id}", message, server=profile.get("server"), profile_id=profile_id)
            messages.append(f"SPID {session_id}: {'terminated' if ok else 'not terminated'} — {message}")
        QMessageBox.information(self, "SQL session termination results", f"Terminated {succeeded} of {len(results)} requested session(s).\n\n" + "\n".join(messages))
        self.refresh_events()

    def record_recovery_evidence(self) -> None:
        profile_id = str(self.database_profile_filter.currentData() or "")
        if not profile_id:
            QMessageBox.information(self, "Select SQL instance", "Select one SQL instance before recording recovery evidence.")
            return
        profile = next((item for item in self.store.get("configuration").get("sql_monitors", []) if str(item.get("id") or item.get("server")) == profile_id), None)
        if not profile:
            return
        dialog = RecoveryEvidenceDialog(str(profile.get("database") or "master"), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.add_sql_recovery_evidence(profile_id, str(profile.get("database") or "master"), dialog.evidence)
            self.store.add_event("info" if dialog.evidence["result"] == "Succeeded" else "error", "sql-recovery-evidence", str(profile.get("database") or "master"), f"{dialog.evidence['evidence_type']} evidence recorded: {dialog.evidence['result']}", owner=dialog.evidence["owner"])
            self.refresh_databases()

    def refresh_databases(self) -> None:
        if not hasattr(self, "database_profile_filter"):
            return
        config = self.store.get("configuration")
        profiles = [profile for profile in config.get("sql_monitors", []) if profile.get("enabled", True)]
        selected_id = self.database_profile_filter.currentData()
        self.database_profile_filter.blockSignals(True)
        self.database_profile_filter.clear()
        self.database_profile_filter.addItem("All configured SQL instances", "")
        for profile in profiles:
            profile_id = str(profile.get("id") or profile.get("server"))
            self.database_profile_filter.addItem(
                str(profile.get("name") or profile.get("server")), profile_id
            )
        selected_index = self.database_profile_filter.findData(selected_id)
        self.database_profile_filter.setCurrentIndex(max(0, selected_index))
        self.database_profile_filter.blockSignals(False)
        selected_id = str(self.database_profile_filter.currentData() or "")
        since_day = (datetime.now(UTC) - timedelta(days=90)).date().isoformat()
        daily_rows = self.store.sql_capacity_daily(since_day, selected_id or None)
        forecasts = capacity_forecasts(daily_rows)
        self.database_capacity_trend.set_samples(daily_rows)
        runtime = self.store.get("sql_runtime", {})
        selected_states = {
            profile_id: state
            for profile_id, state in runtime.items()
            if not selected_id or profile_id == selected_id
        }
        connection_states = [str(state.get("status", "Not checked")) for state in selected_states.values()]
        connection = (
            "Not configured"
            if not profiles
            else "Connected"
            if connection_states and all(value == "Connected" for value in connection_states)
            else "Attention"
        )
        capacity_states = [state.get("capacity", {}) for state in selected_states.values()]
        files = []
        for profile in profiles:
            profile_id = str(profile.get("id") or profile.get("server"))
            if selected_id and profile_id != selected_id:
                continue
            capacity = runtime.get(profile_id, {}).get("capacity", {})
            for item in capacity.get("files", []):
                files.append((profile, item))
        risks = sum(1 for _profile, item in files if item.get("risk"))
        tempdb_states = [
            item.get("tempdb") for item in capacity_states if item.get("tempdb") is not None
        ]
        risks += sum(1 for item in tempdb_states if item.get("status") == "Attention")
        capacity_status = (
            "No data"
            if not capacity_states
            else "No permission"
            if any(item.get("status") == "No permission" for item in capacity_states)
            else "Attention"
            if risks
            else "Healthy"
        )
        self.database_connection_card.findChild(QLabel, "value").setText(connection)
        self.database_capacity_card.findChild(QLabel, "value").setText(capacity_status)
        self.database_file_card.findChild(QLabel, "value").setText(str(len(files)))
        self.database_risk_card.findChild(QLabel, "value").setText(str(risks))
        checked = [item.get("checked_at") for item in capacity_states if item.get("checked_at")]
        errors = [item.get("error") for item in capacity_states if item.get("error")]
        if errors:
            self.database_freshness.setText(
                "Capacity evidence unavailable: " + " | ".join(str(value) for value in errors[:3])
            )
        elif checked:
            tempdb_message = " ".join(
                str(item.get("message", "")) for item in tempdb_states if item.get("message")
            )
            self.database_freshness.setText(
                f"Latest capacity sample: {display_server_time(self.store, max(str(value) for value in checked))}. "
                "Capacity warnings do not mean the SQL connection failed. "
                + tempdb_message
            )
        else:
            self.database_freshness.setText(
                "No capacity sample yet. Confirm SQL monitoring is enabled and allow the independent capacity interval to run."
            )
        self.database_file_table.setRowCount(len(files))
        for row, (profile, item) in enumerate(files):
            growth = (
                f"{item.get('growth_value', 0):.0f}%"
                if item.get("growth_is_percent")
                else f"{item.get('growth_value', 0):,.0f} MB"
            )
            values = (
                f"{profile.get('database', 'master')} / {item.get('logical_name', '')}",
                item.get("file_type", ""),
                f"{item.get('allocated_mb', 0):,.1f} MB",
                "—" if item.get("used_mb") is None else f"{item['used_mb']:,.1f} MB",
                "—" if item.get("free_mb") is None else f"{item['free_mb']:,.1f} MB",
                "—"
                if item.get("utilization_percent") is None
                else f"{item['utilization_percent']:.1f}%",
                growth,
                "Unlimited"
                if item.get("max_size_mb") is None
                else f"{item['max_size_mb']:,.1f} MB",
                item.get("volume_mount") or "Unavailable",
                "—"
                if item.get("volume_free_mb") is None
                else f"{item['volume_free_mb']:,.1f} MB",
                forecast_text(
                    forecasts.get(
                        (
                            str(profile.get("id") or profile.get("server")),
                            str(profile.get("database") or "master"),
                            str(item.get("logical_name", "")),
                        )
                    )
                ),
                item.get("risk") or "Healthy configuration",
            )
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(str(value))
                if column == 11 and item.get("risk"):
                    table_item.setForeground(QColor("#ffb4a2"))
                self.database_file_table.setItem(row, column, table_item)
        backup_states = [state.get("backup_assurance", {}) for state in selected_states.values()]
        backup_checks = [check for state in backup_states for check in state.get("checks", [])]
        backup_status = "No data" if not backup_states else "No permission" if any(state.get("status") == "No permission" for state in backup_states) else "Attention" if any(state.get("status") != "Healthy" for state in backup_states) else "Healthy"
        rpo_values = [state.get("rpo_exposure_hours") for state in backup_states if state.get("rpo_exposure_hours") is not None]
        chains = [str(state.get("chain_status", "No data")) for state in backup_states]
        self.database_backup_sla_card.findChild(QLabel, "value").setText(backup_status)
        self.database_rpo_card.findChild(QLabel, "value").setText("—" if not rpo_values else f"{max(float(value) for value in rpo_values):.1f} hours")
        self.database_chain_card.findChild(QLabel, "value").setText("Gap detected" if "Gap detected" in chains else chains[0] if chains else "No data")
        self.database_history_card.findChild(QLabel, "value").setText(str(sum(int(state.get("history_count", 0)) for state in backup_states)))
        self.database_backup_anomaly_card.findChild(QLabel, "value").setText(str(sum(int(state.get("anomaly_count", 0)) for state in backup_states)))
        calendar = next((state.get("calendar", []) for state in backup_states if state.get("calendar")), [])
        self.database_backup_calendar.set_days(calendar)
        history_rows = [dict(row) for row in self.store.sql_backup_history((datetime.now(UTC) - timedelta(days=31)).isoformat(), selected_id or None)]
        self.database_chain_timeline.set_history(history_rows)
        backup_errors = [str(state.get("error")) for state in backup_states if state.get("error")]
        backup_checked = [str(state.get("checked_at")) for state in backup_states if state.get("checked_at")]
        self.database_backup_freshness.setText("Backup evidence unavailable: " + " | ".join(backup_errors[:3]) if backup_errors else f"Latest SQL-native backup assessment: {display_server_time(self.store, max(backup_checked))}" if backup_checked else "No SQL-native backup evidence collected yet.")
        self.database_backup_table.setRowCount(len(backup_checks))
        for row, check in enumerate(backup_checks):
            age = check.get("age_hours")
            maximum = check.get("maximum_hours")
            baseline = check.get("baseline_anomaly") or (f"Baseline: {check.get('median_duration_seconds', 0):.0f}s / {check.get('median_size_mb', 0):,.1f} MB" if check.get("baseline") == "Established" else "Baseline needs more history")
            verification = "Checksum included" if check.get("has_checksums") else "No verification evidence"
            values = (check.get("label", ""), "Yes" if check.get("required") else "No", check.get("status", ""), check.get("latest") or "—", "—" if age is None else f"{float(age):.1f} h", f"{float(maximum):.1f} h", check.get("destination") or "—", f"{verification} · {baseline}")
            for column, value in enumerate(values):
                table_item = QTableWidgetItem(str(value))
                if column == 2 and check.get("status") in ("Late", "Missing"):
                    table_item.setForeground(QColor("#ffb4a2"))
                self.database_backup_table.setItem(row, column, table_item)
        evidence_rows = self.store.sql_recovery_evidence(selected_id or None)
        readiness = recovery_readiness(backup_states[0] if len(backup_states) == 1 else {"status": backup_status, "chain_status": "Gap detected" if "Gap detected" in chains else "Continuous"}, [dict(row) for row in evidence_rows])
        self.database_readiness_card.findChild(QLabel, "value").setText(readiness["status"])
        self.database_readiness_card.setToolTip("; ".join(readiness["reasons"]) or "Backup SLA, chain, and recent recovery evidence are healthy")
        self.database_recovery_table.setRowCount(len(evidence_rows))
        for row, evidence in enumerate(evidence_rows):
            values = (str(evidence["performed_at"])[:10], evidence["database_name"], evidence["evidence_type"], evidence["result"], evidence["owner"], evidence["destination"] or "—", "—" if evidence["duration_minutes"] is None else f"{evidence['duration_minutes']} min", evidence["notes"])
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 3 and value == "Failed": item.setForeground(QColor("#ffb4a2"))
                self.database_recovery_table.setItem(row, column, item)

    def reports_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Client analytics reports",
            "Generate branded infrastructure analytics for this month or a specific duration, then export to PDF or Word.",
        )
        controls = QHBoxLayout()
        self.analytics_period = QComboBox()
        self.analytics_period.addItems(["This month", "Custom duration"])
        self.analytics_start = QDateEdit(QDate.currentDate().addDays(1 - QDate.currentDate().day()))
        self.analytics_end = QDateEdit(QDate.currentDate())
        for editor in (self.analytics_start, self.analytics_end):
            editor.setCalendarPopup(True); editor.setDisplayFormat("dd MMM yyyy")
        generate = QPushButton("Generate preview")
        generate.setObjectName("primary"); generate.clicked.connect(self.refresh_analytics_report)
        pdf = QPushButton("Export PDF…"); pdf.clicked.connect(self.export_analytics_pdf)
        word = QPushButton("Export Word…"); word.clicked.connect(self.export_analytics_word)
        email = QPushButton("Queue PDF email"); email.clicked.connect(self.email_analytics_report)
        for widget in (self.analytics_period, self.analytics_start, self.analytics_end, generate, pdf, word, email):
            controls.addWidget(widget)
        controls.addStretch(); layout.addLayout(controls)
        self.analytics_status = QLabel("Choose a duration and generate the report preview.")
        self.analytics_status.setWordWrap(True); self.analytics_status.setStyleSheet("color:#98a6aa")
        layout.addWidget(self.analytics_status)
        self.analytics_summary = self.table(["Section", "Period result", "Client interpretation"])
        layout.addWidget(self.analytics_summary, 1)
        sections = QLabel(
            "Report sections: 1 Duration overview summary  ·  2 Server health, database growth, and backup charts  ·  "
            "3 Database health and 31-day backup validation  ·  4 Incidents and warnings  ·  "
            "5 Services stability  ·  6 Pools stability  ·  7 Business impact  ·  8 Next actions"
        )
        sections.setWordWrap(True); sections.setStyleSheet("color:#98a6aa")
        layout.addWidget(sections)
        return page

    def _analytics_period_value(self) -> ReportPeriod:
        if self.analytics_period.currentText() == "This month":
            return this_month(self.store)
        config = self.store.get("configuration")
        zone = server_timezone(config)
        start = self.analytics_start.date().toPython()
        end = self.analytics_end.date().toPython()
        if end < start:
            start, end = end, start
        return ReportPeriod(
            datetime.combine(start, dt_time.min, zone), datetime.combine(end, dt_time.max, zone)
        )

    def refresh_analytics_report(self) -> None:
        report = build_analytics(self.store, self._analytics_period_value())
        rows = [
            ("Duration overview", report["period"].label, f"{len(report['events'])} recorded event(s)"),
            ("Server health", f"{report['average_health']:.1f}/100", "Average sampled infrastructure health"),
            ("Database growth", f"{len(report['databases'])} database(s)", "Start and end allocated size comparison"),
            ("Backup validation", f"{report['backup_rate']:.1f}% passed", f"{len(report['backups'])} recorded validation(s)"),
            ("Incidents and warnings", str(len(report["incidents"])), f"{len(report['recoveries'])} recovery event(s)"),
            ("Services stability", f"{len(report['services'])} monitored", "Availability estimated from failure and recovery events"),
            ("Pools stability", f"{len(report['pools'])} monitored", "Availability estimated from failure and recovery events"),
            ("Business impact and actions", f"{report['availability']:.2f}% availability", f"{len(report['next_actions'])} recommended next action(s)"),
        ]
        self.analytics_summary.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values): self.analytics_summary.setItem(row, column, QTableWidgetItem(str(value)))
        self.analytics_status.setText(
            f"Preview ready for {report['period'].label}. Exports include all eight sections and three branded charts."
        )

    def export_analytics_pdf(self) -> None:
        period = self._analytics_period_value()
        filename, _ = QFileDialog.getSaveFileName(self, "Export client analytics PDF", f"Client-Analytics-{period.start:%Y-%m-%d}.pdf", "PDF report (*.pdf)")
        if not filename: return
        try:
            export_pdf(self.store, period, Path(filename))
        except Exception as exc:  # noqa: BLE001 - surface export failures in the GUI
            QMessageBox.critical(self, "PDF export failed", str(exc))
            return
        QMessageBox.information(self, "PDF exported", f"Client report saved to:\n{filename}")

    def export_analytics_word(self) -> None:
        period = self._analytics_period_value()
        filename, _ = QFileDialog.getSaveFileName(self, "Export client analytics Word report", f"Client-Analytics-{period.start:%Y-%m-%d}.docx", "Word report (*.docx)")
        if not filename: return
        try:
            export_docx(self.store, period, Path(filename))
        except Exception as exc:  # noqa: BLE001 - surface export failures in the GUI
            QMessageBox.critical(self, "Word export failed", str(exc))
            return
        QMessageBox.information(self, "Word report exported", f"Client report saved to:\n{filename}")

    def email_analytics_report(self) -> None:
        period = self._analytics_period_value(); config = self.store.get("configuration")
        recipients = config.get("reports", {}).get("recipients") or config["email"].get("recipients", [])
        if not recipients:
            QMessageBox.warning(self, "Report recipients required", "Add report or notification recipients in Configuration first."); return
        folder = self.store.path.parent / "reports"
        filename = folder / f"Client-Analytics-{period.start:%Y-%m-%d}-{period.end:%Y-%m-%d}.pdf"
        try:
            export_pdf(self.store, period, filename)
        except Exception as exc:  # noqa: BLE001 - surface export failures in the GUI
            QMessageBox.critical(self, "Report generation failed", str(exc))
            return
        body = f"<p>The attached client infrastructure analytics report covers <b>{period.label}</b> and includes all eight operational sections.</p>"
        Notifier(self.store).enqueue("Client Infrastructure Analytics Report", branded_html("Client infrastructure analytics report", body), recipients, [str(filename)])
        QMessageBox.information(self, "Report queued", f"The PDF report was queued for {', '.join(recipients)}.")

    def refresh_assurance_rules(self) -> None:
        config = self.store.get("configuration")
        sql_profiles = config.get("sql_monitors", [])
        sql_runtime = self.store.get("sql_runtime", {})
        self.sql_table.setRowCount(len(sql_profiles))
        for row, profile in enumerate(sql_profiles):
            runtime = sql_runtime.get(str(profile.get("id") or profile.get("server")), {})
            job_summary = ", ".join(
                f"{name}: {value.get('status', '—')}"
                for name, value in runtime.get("jobs", {}).items()
            ) or "Not checked yet"
            values = (
                profile.get("name", ""), profile.get("server", ""),
                profile.get("database", "master"), runtime.get("status", "Not checked yet"),
                str(len(profile.get("jobs", []))), job_summary,
                "Yes" if profile.get("enabled", True) else "No",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(profile.get("id", "")))
                self.sql_table.setItem(row, column, item)
        backups = config.get("backups", [])
        backup_runtime = self.store.get("backup_runtime", {})
        self.backup_table.setRowCount(len(backups))
        for row, rule in enumerate(backups):
            rule_id = str(rule.get("id") or rule.get("name") or rule.get("path"))
            evidence = backup_runtime.get(rule_id, {})
            result = evidence.get("result", {})
            if result:
                latest = result.get("file") or "Evidence found"
                status = "Healthy" if result.get("ok") else "Needs attention"
                reason = str(result.get("reason") or "")
            else:
                latest, status, reason = "Not checked yet", "Pending", ""
            acknowledged_at = str(rule.get("acknowledged_at") or "")
            checked_at = str(evidence.get("checked_at") or "")
            acknowledged = acknowledged_at if acknowledged_at and str(rule.get("acknowledged_evidence_at") or "") == checked_at else "—"
            values = (rule.get("name", ""), rule.get("path", ""), rule.get("pattern", "*.bak"), rule.get("check_time", "06:00"), f"{int(rule.get('max_age_days', 0))} day(s)", latest, status, acknowledged, "Yes" if rule.get("enabled", True) else "No")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value)); item.setToolTip(reason)
                if column == 0: item.setData(Qt.ItemDataRole.UserRole, rule_id)
                if column == 6 and status == "Needs attention": item.setForeground(QColor("#ffb4a2"))
                self.backup_table.setItem(row, column, item)
        files = config.get("file_monitors", [])
        self.file_table.setRowCount(len(files))
        for row, rule in enumerate(files):
            for column, value in enumerate(
                (rule.get("name", ""), rule.get("path", ""), "Yes" if rule.get("enabled", True) else "No")
            ):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(rule.get("id", "")))
                self.file_table.setItem(row, column, item)

    def edit_sql_monitor(self, row: int = -1) -> None:
        config = self.store.get("configuration")
        profiles = config.get("sql_monitors", [])
        existing = profiles[row] if 0 <= row < len(profiles) else None
        dialog = SqlMonitorDialog(existing, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if existing is None:
            profiles.append(dialog.profile)
        else:
            profiles[row] = dialog.profile
        config["sql_monitors"] = profiles
        self.store.set("configuration", config)
        self.store.add_event(
            "info", "configuration", dialog.profile["name"], "SQL monitoring profile saved"
        )
        self.refresh_assurance_rules()

    def remove_sql_monitor(self) -> None:
        row = self.sql_table.currentRow()
        if row < 0:
            return
        config = self.store.get("configuration")
        config.get("sql_monitors", []).pop(row)
        self.store.set("configuration", config)
        self.refresh_assurance_rules()

    def edit_backup_rule(self, row: int = -1) -> None:
        config = self.store.get("configuration")
        rules = config.get("backups", [])
        existing = rules[row] if 0 <= row < len(rules) else None
        dialog = BackupRuleDialog(existing, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if existing is None:
            rules.append(dialog.rule)
        else:
            rules[row] = dialog.rule
        config["backups"] = rules
        self.store.set("configuration", config)
        self.store.add_event("info", "configuration", dialog.rule["name"], "Backup rule saved")
        self.refresh_assurance_rules()

    def remove_backup_rule(self) -> None:
        row = self.backup_table.currentRow()
        if row < 0:
            return
        config = self.store.get("configuration")
        rule = config.get("backups", []).pop(row)
        self.store.set("configuration", config)
        self.store.resolve_score_incident_by_key(f"backup:{rule.get('id')}", "Rule removed")
        self.refresh_assurance_rules()

    def acknowledge_backup_rule(self) -> None:
        row = self.backup_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select backup check", "Select one backup-file check first.")
            return
        config = self.store.get("configuration")
        rules = config.get("backups", [])
        if row >= len(rules): return
        rule = rules[row]
        rule_id = str(rule.get("id") or rule.get("name") or rule.get("path"))
        evidence = self.store.get("backup_runtime", {}).get(rule_id, {})
        if not evidence.get("result"):
            QMessageBox.warning(self, "No evidence to acknowledge", "Run this backup check before acknowledging its result.")
            return
        rule["acknowledged_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        rule["acknowledged_evidence_at"] = str(evidence.get("checked_at") or "")
        operator = str(config.get("client", {}).get("operator_email") or "Unknown operator")
        config["backups"] = rules; self.store.set("configuration", config)
        self.store.add_event("info", "backup-acknowledgement", str(rule.get("name") or rule.get("path")), f"Backup-file evidence from {evidence.get('checked_at')} acknowledged by {operator}", path=rule.get("path"), evidence=evidence.get("result"))
        self.refresh_assurance_rules()

    def check_backup_rule_now(self) -> None:
        row = self.backup_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select backup check", "Select one backup-file check first.")
            return
        config = self.store.get("configuration"); rules = config.get("backups", [])
        if row >= len(rules): return
        rule = rules[row]
        result = AssuranceMonitor(self.store, Notifier(self.store)).check_backup_rule(rule, manual=True)
        self.refresh_assurance_rules()
        QMessageBox.information(self, "Backup check complete", str(result.get("reason") or "Check completed."))

    def edit_file_rule(self, row: int = -1) -> None:
        config = self.store.get("configuration")
        rules = config.get("file_monitors", [])
        existing = rules[row] if 0 <= row < len(rules) else None
        dialog = FileMonitorDialog(existing, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if existing is None:
            rules.append(dialog.rule)
        else:
            rules[row] = dialog.rule
        config["file_monitors"] = rules
        self.store.set("configuration", config)
        self.store.add_event("info", "configuration", dialog.rule["name"], "File monitor saved")
        self.refresh_assurance_rules()

    def remove_file_rule(self) -> None:
        row = self.file_table.currentRow()
        if row < 0:
            return
        config = self.store.get("configuration")
        config.get("file_monitors", []).pop(row)
        self.store.set("configuration", config)
        self.refresh_assurance_rules()

    def open_component_changes(self) -> None:
        ComponentChangesDialog(self.store, self).exec()

    @staticmethod
    def coverage_row(layout: QVBoxLayout, caption: str) -> tuple[QLabel, QProgressBar]:
        row = QHBoxLayout()
        label = QLabel(caption)
        value = QLabel("—")
        value.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(label)
        row.addStretch()
        row.addWidget(value)
        progress = QProgressBar()
        progress.setRange(0, 100)
        layout.addLayout(row)
        layout.addWidget(progress)
        return value, progress

    @staticmethod
    def add_dashboard_item(
        widget: QListWidget,
        title: str,
        detail: str,
        *,
        target: str | None = None,
        color: str = "#e6ecee",
    ) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, target)
        item.setSizeHint(QSize(0, 58))
        content = QWidget()
        box = QVBoxLayout(content)
        box.setContentsMargins(8, 5, 8, 5)
        box.setSpacing(2)
        heading = QLabel(title)
        heading.setStyleSheet(f"color:{color};font-weight:700")
        body = QLabel(detail)
        body.setWordWrap(True)
        body.setStyleSheet("color:#b4bec1")
        box.addWidget(heading)
        box.addWidget(body)
        widget.addItem(item)
        widget.setItemWidget(item, content)

    def targets_page(self, kind: str) -> QWidget:
        title = "Windows Services" if kind == "service" else "IIS Application Pools"
        page, layout = self.page_shell(
            title,
            "Monitored inventory · every item has independent recovery, scheduling, maintenance, and API health rules.",
        )
        bar = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText(f"Filter monitored {title.lower()}…")
        discover = QPushButton("+ Add items")
        discover.setObjectName("primary")
        remove_selected = QPushButton("Remove selected")
        remove_selected.setObjectName("danger")
        bar.addWidget(search, 1)
        bar.addWidget(remove_selected)
        bar.addWidget(discover)
        layout.addLayout(bar)
        summary = QLabel()
        summary.setStyleSheet("color:#98a6aa")
        layout.addWidget(summary)
        table = self.table(
            ["State", "Item", "Monitoring", "Health", "Recovery", "Schedule", "Last event", "Actions"]
        )
        table.setObjectName(kind)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(table, 1)
        search.textChanged.connect(lambda text: self.filter_table(table, text))
        discover.clicked.connect(lambda: self.open_discovery(kind, table, summary))
        remove_selected.clicked.connect(
            lambda: self.remove_selected_targets(kind, table, summary)
        )
        setattr(self, f"{kind}_table", table)
        setattr(self, f"{kind}_summary", summary)
        self.refresh_target_inventory(kind, table, summary)
        return page

    def remove_selected_targets(
        self, kind: str, table: QTableWidget, summary: QLabel
    ) -> None:
        keys = {
            str(table.item(index.row(), 1).data(Qt.ItemDataRole.UserRole))
            for index in table.selectionModel().selectedRows()
            if table.item(index.row(), 1) is not None
            and table.item(index.row(), 1).data(Qt.ItemDataRole.UserRole)
        }
        if not keys:
            QMessageBox.information(
                self, "No items selected", "Select one or more rows to remove."
            )
            return
        noun = "Windows service" if kind == "service" else "IIS pool"
        if (
            QMessageBox.question(
                self,
                "Remove monitored items",
                f"Remove {len(keys)} selected {noun}(s) and their monitoring configurations?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        with self.store.connect() as db:
            db.executemany(
                "DELETE FROM targets WHERE kind=? AND target_key=?",
                [(kind, key) for key in keys],
            )
        for key in keys:
            self.store.deactivate_target_alerts(kind, key)
        self.store.add_event(
            "info",
            "configuration",
            kind,
            f"Removed {len(keys)} selected {noun}(s) from monitoring",
            targets=sorted(keys),
        )
        self.refresh_target_inventory(kind, table, summary)

    def open_discovery(self, kind: str, table: QTableWidget, summary: QLabel) -> None:
        if DiscoveryDialog(self.store, kind, self).exec() == QDialog.DialogCode.Accepted:
            self.refresh_target_inventory(kind, table, summary)

    def refresh_target_inventory(
        self, kind: str, table: QTableWidget, summary: QLabel | None = None
    ) -> None:
        rows = self.store.targets(kind)
        live_items = windows_services() if kind == "service" else iis_pools()
        live = {item["key"]: item for item in live_items}
        table.setRowCount(len(rows))
        enabled_count = sum(bool(row["enabled"]) for row in rows)
        if summary is not None:
            summary.setText(
                f"{len(rows)} configured · {enabled_count} actively monitored · use Actions to configure one item"
                if rows
                else "No items added yet. Choose + Add items to build the monitored inventory."
            )
        for row_index, row in enumerate(rows):
            key = row["target_key"]
            current = live.get(key, {})
            state = str(current.get("status", "Unavailable"))
            config = json.loads(row["config"] or "{}")
            enabled = bool(row["enabled"])
            state_item = QTableWidgetItem(state)
            state_item.setForeground(
                QColor("#58d68d")
                if state.lower() in {"running", "started"}
                else QColor("#f2b84b")
            )
            table.setItem(row_index, 0, state_item)
            name_item = QTableWidgetItem(str(row["display_name"]))
            name_item.setToolTip(key)
            name_item.setData(Qt.ItemDataRole.UserRole, key)
            table.setItem(row_index, 1, name_item)
            toggle = QCheckBox()
            toggle.setChecked(enabled)
            toggle.setToolTip("Pause or resume checks, actions, and alerts for this item")
            toggle.stateChanged.connect(
                lambda value, target_key=key: self.toggle_monitoring(kind, target_key, value)
            )
            table.setCellWidget(row_index, 2, toggle)
            health = (
                "Paused"
                if not enabled
                else ("Healthy" if state.lower() in {"running", "started"} else "Attention")
            )
            table.setItem(row_index, 3, QTableWidgetItem(health))
            recovery = (
                f"Auto · {config.get('retry_count', 3)} retries"
                if config.get("start_if_stopped", config.get("auto_restart", True))
                else "Notify only"
            )
            table.setItem(row_index, 4, QTableWidgetItem(recovery))
            schedule = config.get("schedule", {})
            schedule_text = (
                f"{schedule.get('frequency', 'Daily')} · "
                f"{schedule.get('time', schedule.get('interval_minutes', ''))}"
                if schedule.get("enabled")
                else "Not scheduled"
            )
            table.setItem(row_index, 5, QTableWidgetItem(schedule_text))
            events = self.store.target_events(key, 1)
            last_event = (
                f"{events[0]['created_at'][11:19]} · {events[0]['message']}"
                if events
                else "No events"
            )
            table.setItem(row_index, 6, QTableWidgetItem(last_event))
            actions = QComboBox()
            action_names = ["Actions…", "Configure", "History", "Start", "Stop"]
            if kind == "pool":
                action_names.append("Recycle")
            action_names.append("Remove")
            actions.addItems(action_names)
            actions.currentTextChanged.connect(
                lambda action, target_key=key, widget=actions: self.inventory_action(
                    kind, target_key, action, widget
                )
            )
            table.setCellWidget(row_index, 7, actions)

    def inventory_action(
        self, kind: str, key: str, action: str, widget: QComboBox
    ) -> None:
        if action == "Configure":
            self.configure_target(kind, key)
        elif action == "History":
            TargetHistoryDialog(self.store, key, self).exec()
        elif action in {"Start", "Stop", "Recycle"}:
            self.control_target(kind, key, action)
        elif action == "Remove" and (
            QMessageBox.question(
                self,
                "Remove monitored item",
                f"Remove {key} and its monitoring configuration?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            with self.store.connect() as db:
                db.execute(
                    "DELETE FROM targets WHERE kind=? AND target_key=?", (kind, key)
                )
            self.store.deactivate_target_alerts(kind, key)
            self.store.add_event(
                "info", "configuration", key, "Item removed from monitoring"
            )
        widget.blockSignals(True)
        widget.setCurrentIndex(0)
        widget.blockSignals(False)
        self.refresh_target_inventory(
            kind, getattr(self, f"{kind}_table"), getattr(self, f"{kind}_summary")
        )

    def load_selected_targets(self, kind: str, table: QTableWidget) -> None:
        items = [
            {"key": row["target_key"], "name": row["display_name"], "status": "Saved target"}
            for row in self.store.targets(kind)
        ]
        self.populate_target_table(kind, table, items)

    def disk_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Disk capacity", "Current free space and monitoring state for every detected drive."
        )
        self.monitor_all_disks = QCheckBox("Monitor every detected drive automatically")
        self.monitor_all_disks.setToolTip(
            "Turn this off to choose individual drives in the table below."
        )
        self.monitor_all_disks.setChecked(
            self.store.get("configuration")["disk"].get("monitor_all", True)
        )
        self.monitor_all_disks.stateChanged.connect(self.toggle_monitor_all_disks)
        layout.addWidget(self.monitor_all_disks)
        self.disk_table = self.table(["Monitor", "Drive", "Free MB", "Free %", "State"])
        layout.addWidget(self.disk_table, 1)
        return page

    def performance_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Performance intelligence",
            "Start with server health, then open SQL Server evidence only when deeper analysis is needed.",
        )
        domains = QTabWidget(); domains.setDocumentMode(True); self.performance_domains = domains
        server_page = QWidget(); server_layout = QVBoxLayout(server_page)
        sql_page = QWidget(); sql_layout = QVBoxLayout(sql_page)
        domains.addTab(server_page, "Server")
        domains.addTab(sql_page, "SQL Server")
        layout.addWidget(domains, 1)
        cards = QGridLayout()
        self.performance_cpu_card = self.card("Live CPU", "—")
        self.performance_memory_card = self.card("Live memory", "—")
        self.performance_peak_card = self.card("24h peak", "—")
        self.performance_cause_card = self.card("Top consumer", "—")
        for index, card in enumerate(
            (
                self.performance_cpu_card,
                self.performance_memory_card,
                self.performance_peak_card,
                self.performance_cause_card,
            )
        ):
            cards.addWidget(card, 0, index)
        server_layout.addLayout(cards)
        upper = QGridLayout()
        trend_card = QFrame()
        trend_card.setObjectName("card")
        trend_box = QVBoxLayout(trend_card)
        trend_title = QLabel("SERVER PERFORMANCE · LAST 24 HOURS")
        trend_title.setObjectName("eyebrow")
        trend_box.addWidget(trend_title)
        self.performance_trend = TrendWidget()
        trend_box.addWidget(self.performance_trend)
        live_card = QFrame()
        live_card.setObjectName("card")
        live_box = QVBoxLayout(live_card)
        live_title = QLabel("CURRENT TOP RESOURCE CONSUMERS")
        live_title.setObjectName("eyebrow")
        live_box.addWidget(live_title)
        self.performance_root_cause = QLabel("Collecting process attribution…")
        self.performance_root_cause.setWordWrap(True)
        self.performance_root_cause.setStyleSheet("color:#98a6aa")
        live_box.addWidget(self.performance_root_cause)
        self.performance_consumers_table = self.table(
            ["Type", "Item", "PID", "CPU", "Memory"]
        )
        live_box.addWidget(self.performance_consumers_table)
        upper.addWidget(trend_card, 0, 0)
        upper.addWidget(live_card, 0, 1)
        upper.setColumnStretch(0, 1)
        upper.setColumnStretch(1, 1)
        server_layout.addLayout(upper, 2)
        peaks_card = QFrame()
        peaks_card.setObjectName("card")
        peaks_box = QVBoxLayout(peaks_card)
        peaks_title = QLabel("PEAK TIMELINE · LIKELY ROOT CAUSE AT EACH SPIKE")
        peaks_title.setObjectName("eyebrow")
        peaks_box.addWidget(peaks_title)
        self.performance_peaks_table = self.table(
            ["Time", "CPU", "Memory", "Likely root cause"]
        )
        peaks_box.addWidget(self.performance_peaks_table)
        server_layout.addWidget(peaks_card, 1)
        sql_header = QHBoxLayout(); sql_title = QLabel("SQL SERVER PERFORMANCE")
        sql_title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold)); self.sql_performance_profile = QComboBox()
        self.sql_performance_profile.currentIndexChanged.connect(self.refresh_performance)
        sql_header.addWidget(sql_title); sql_header.addStretch(); sql_header.addWidget(QLabel("Instance")); sql_header.addWidget(self.sql_performance_profile)
        sql_layout.addLayout(sql_header)
        sql_cards = QGridLayout()
        self.sql_cpu_card = self.card("SQL process CPU", "No data")
        self.sql_sessions_card = self.card("Active sessions", "No data")
        self.sql_blocking_card = self.card("Blocking", "No data")
        self.sql_grants_card = self.card("Memory grants", "No data")
        for index, card in enumerate((self.sql_cpu_card, self.sql_sessions_card, self.sql_blocking_card, self.sql_grants_card)): sql_cards.addWidget(card, 0, index)
        sql_layout.addLayout(sql_cards)
        self.sql_performance_freshness = QLabel("No SQL performance sample yet."); self.sql_performance_freshness.setWordWrap(True); self.sql_performance_freshness.setStyleSheet("color:#98a6aa")
        sql_layout.addWidget(self.sql_performance_freshness)
        sql_details = QTabWidget(); sql_details.setDocumentMode(True); self.sql_performance_details = sql_details
        overview = QWidget(); overview_layout = QVBoxLayout(overview)
        io_page = QWidget(); io_layout = QVBoxLayout(io_page)
        query_page = QWidget(); query_layout = QVBoxLayout(query_page)
        deadlock_page = QWidget(); deadlock_layout = QVBoxLayout(deadlock_page)
        changes_page = QWidget(); changes_layout = QVBoxLayout(changes_page)
        sql_details.addTab(overview, "Overview")
        sql_details.addTab(io_page, "I/O & waits")
        sql_details.addTab(query_page, "Blocking & active queries")
        sql_details.addTab(deadlock_page, "Deadlocks")
        sql_details.addTab(changes_page, "Configuration changes")
        sql_layout.addWidget(sql_details, 1)
        overview_note = QLabel("SQL CPU and active-session trend · the chart appears after multiple samples.")
        overview_note.setStyleSheet("color:#98a6aa"); overview_layout.addWidget(overview_note)
        self.sql_performance_trend = SqlPerformanceTrendWidget(); overview_layout.addWidget(self.sql_performance_trend, 1)
        self.sql_io_empty = QLabel("No I/O or wait contributors are available yet. Collection remains lightweight and runs on the configured interval.")
        self.sql_io_empty.setWordWrap(True); self.sql_io_empty.setAlignment(Qt.AlignmentFlag.AlignCenter); self.sql_io_empty.setStyleSheet("color:#98a6aa;padding:30px")
        io_layout.addWidget(self.sql_io_empty)
        self.sql_io_wait_table = self.table(["Contributor", "Read latency", "Write latency", "Read MB", "Write MB / wait"])
        io_layout.addWidget(self.sql_io_wait_table, 1)
        query_tools = QHBoxLayout(); query_tools.addWidget(QLabel("LIVE USER SESSIONS & BLOCKING"))
        self.sql_session_filter = QComboBox()
        self.sql_session_filter.addItems(["All sessions", "Blocked", "Blockers", "Running", "Runnable", "Suspended", "Sleeping", "Dormant"])
        self.sql_session_filter.currentTextChanged.connect(self.refresh_performance)
        self.sql_session_search = QLineEdit(); self.sql_session_search.setPlaceholderText("Filter session, login, host, application, database, source, or query…")
        self.sql_session_search.textChanged.connect(self.refresh_performance)
        self.sql_kill_button = QPushButton("Kill selected sessions…"); self.sql_kill_button.setObjectName("danger"); self.sql_kill_button.setEnabled(False); self.sql_kill_button.clicked.connect(self.kill_selected_sql_sessions)
        query_tools.addStretch(); query_tools.addWidget(QLabel("Show")); query_tools.addWidget(self.sql_session_filter); query_tools.addWidget(self.sql_session_search, 1); query_tools.addWidget(self.sql_kill_button); query_layout.addLayout(query_tools)
        query_explanation = QLabel("All user sessions are shown from the latest interval. Only blockers and blocked sessions are persisted. Select multiple rows to terminate sessions after confirmation; system and monitor sessions are protected.")
        query_explanation.setWordWrap(True); query_explanation.setStyleSheet("color:#98a6aa")
        query_layout.addWidget(query_explanation)
        self.sql_session_freshness = QLabel("No session inventory has been collected yet.")
        self.sql_session_freshness.setWordWrap(True); self.sql_session_freshness.setStyleSheet("color:#98a6aa"); query_layout.addWidget(self.sql_session_freshness)
        self.sql_sessions_table = self.table(["SPID", "Type", "Status", "Blocked by", "Database", "Login", "Host", "Application", "Wait", "Transaction", "Source", "Query content"])
        self.sql_sessions_table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.sql_sessions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sql_sessions_table.horizontalHeader().setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)
        query_layout.addWidget(self.sql_sessions_table, 2)
        history_title = QLabel("PERSISTED BLOCKING AND LONG-RUNNING EVIDENCE"); history_title.setObjectName("eyebrow"); query_layout.addWidget(history_title)
        self.sql_query_empty = QLabel("No blocking or long-running query evidence has been collected.")
        self.sql_query_empty.setWordWrap(True); self.sql_query_empty.setAlignment(Qt.AlignmentFlag.AlignCenter); self.sql_query_empty.setStyleSheet("color:#98a6aa;padding:30px")
        query_layout.addWidget(self.sql_query_empty)
        self.sql_query_table = self.table(["Time", "SPID", "Role", "Blocked by", "Status / wait", "Database", "Source", "Query content"])
        self.sql_query_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        query_layout.addWidget(self.sql_query_table, 1)
        self.sql_deadlock_empty = QLabel("No deadlock evidence has been collected. Deadlock collection uses SQL Server event-time evidence and never enables a profiler trace.")
        self.sql_deadlock_empty.setWordWrap(True); self.sql_deadlock_empty.setAlignment(Qt.AlignmentFlag.AlignCenter); self.sql_deadlock_empty.setStyleSheet("color:#98a6aa;padding:30px")
        deadlock_layout.addWidget(self.sql_deadlock_empty)
        self.sql_deadlock_table = self.table(["Time", "Victim", "Database / object", "Source", "Root cause", "Evidence"])
        self.sql_deadlock_table.hide(); deadlock_layout.addWidget(self.sql_deadlock_table, 1)
        self.sql_changes_empty = QLabel("No SQL Server or database configuration change has been detected. The first successful collection establishes the approved baseline.")
        self.sql_changes_empty.setWordWrap(True); self.sql_changes_empty.setAlignment(Qt.AlignmentFlag.AlignCenter); self.sql_changes_empty.setStyleSheet("color:#98a6aa;padding:30px")
        changes_layout.addWidget(self.sql_changes_empty)
        self.sql_changes_table = self.table(["Detected", "Scope", "Setting", "Previous", "Current", "Status"])
        changes_layout.addWidget(self.sql_changes_table, 1)
        return page

    def events_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Events & audit trail",
            "Normal operations, warnings, recoveries, and every automatic action in one timeline.",
        )
        filters = QHBoxLayout()
        self.event_severity = QComboBox()
        self.event_severity.addItems(["All", "Critical", "Error", "Warning", "Recovery", "Info"])
        self.event_period = QComboBox()
        self.event_period.addItems(["All time", "Last 24 hours", "Last 7 days", "Last 30 days"])
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText("Search target, category, or message…")
        export = QPushButton("Export CSV…")
        self.event_severity.currentTextChanged.connect(self.refresh_events)
        self.event_period.currentTextChanged.connect(self.refresh_events)
        self.event_search.textChanged.connect(self.refresh_events)
        export.clicked.connect(self.export_events)
        filters.addWidget(self.event_severity)
        filters.addWidget(self.event_period)
        filters.addWidget(self.event_search, 1)
        filters.addWidget(export)
        layout.addLayout(filters)
        self.event_table = self.table(["Time", "Severity", "Category", "Target", "Message"])
        layout.addWidget(self.event_table, 1)
        return page

    def settings_page(self) -> QWidget:
        page, layout = self.page_shell(
            "Configuration", "Monitoring, reports, email validation, and agent lifecycle."
        )
        config = self.store.get("configuration")
        tabs = QTabWidget()
        monitoring_tab = QWidget()
        form = QFormLayout(monitoring_tab)
        form.setContentsMargins(22, 24, 22, 24)
        form.setSpacing(14)
        self.cpu_threshold = QSpinBox()
        self.cpu_threshold.setRange(1, 100)
        self.cpu_threshold.setValue(config["performance"]["cpu_percent"])
        self.cpu_threshold.setSuffix(" %")
        self.memory_threshold = QSpinBox()
        self.memory_threshold.setRange(1, 100)
        self.memory_threshold.setValue(config["performance"]["memory_percent"])
        self.memory_threshold.setSuffix(" %")
        self.disk_threshold = QSpinBox()
        self.disk_threshold.setRange(1, 99)
        self.disk_threshold.setValue(config["disk"]["minimum_free_percent"])
        self.disk_threshold.setSuffix(" % free")
        self.sustain_seconds = QSpinBox()
        self.sustain_seconds.setRange(10, 3600)
        self.sustain_seconds.setValue(config["performance"].get("sustain_seconds", 120))
        self.sustain_seconds.setSuffix(" seconds")
        self.sustain_seconds.setToolTip(
            "CPU or memory must remain above its threshold for this duration before alerting."
        )
        self.automatic_actions = QCheckBox("Allow guarded automatic and scheduled actions")
        self.automatic_actions.setChecked(config["agent"].get("automatic_actions", True))
        self.debug = QCheckBox("Enable detailed diagnostic logging")
        self.debug.setChecked(config["agent"].get("debug", False))
        self.close_to_tray = QCheckBox("Hide the console in the system tray when closed")
        self.close_to_tray.setChecked(config["agent"].get("close_to_tray", True))
        self.start_minimized = QCheckBox("Start the console minimized in the system tray")
        self.start_minimized.setChecked(config["agent"].get("start_minimized", False))
        self.timezone = QLineEdit(config["agent"].get("timezone", "SERVER_LOCAL"))
        self.timezone.setPlaceholderText("SERVER_LOCAL or example: Africa/Cairo")
        self.timezone.setToolTip("SERVER_LOCAL uses the Windows server clock. Enter an IANA timezone only when explicitly required.")
        diagnostics = config.get("diagnostics", {})
        self.retention_days = QSpinBox()
        self.retention_days.setRange(1, 3650)
        self.retention_days.setValue(int(diagnostics.get("retention_days", 30)))
        self.retention_days.setSuffix(" days")
        self.maximum_events = QSpinBox()
        self.maximum_events.setRange(1000, 1000000)
        self.maximum_events.setSingleStep(1000)
        self.maximum_events.setValue(int(diagnostics.get("max_events", 100000)))
        self.trend_hours = QSpinBox()
        self.trend_hours.setRange(1, 720)
        self.trend_hours.setValue(int(diagnostics.get("trend_hours", 24)))
        self.trend_hours.setSuffix(" hours")
        form.addRow("CPU warning threshold", self.cpu_threshold)
        form.addRow("Memory warning threshold", self.memory_threshold)
        form.addRow("Disk warning threshold", self.disk_threshold)
        form.addRow("Sustained breach duration", self.sustain_seconds)
        form.addRow("Automatic actions", self.automatic_actions)
        form.addRow("Server timezone", self.timezone)
        form.addRow("Diagnostic retention", self.retention_days)
        form.addRow("Maximum audit events", self.maximum_events)
        form.addRow("Dashboard trend period", self.trend_hours)
        form.addRow("Debug mode", self.debug)
        form.addRow("System tray", self.close_to_tray)
        form.addRow("Startup visibility", self.start_minimized)
        tabs.addTab(monitoring_tab, "Monitoring")

        reports_tab = QWidget()
        reports_form = QFormLayout(reports_tab)
        reports_form.setContentsMargins(22, 24, 22, 24)
        reports_form.setSpacing(14)
        reports = config.get("reports", {})
        self.notification_recipients = QLineEdit(
            ", ".join(config["email"].get("recipients", []))
        )
        self.notification_recipients.setPlaceholderText(
            "Required: operator@millensys.com, support@millensys.com"
        )
        self.notification_recipients.setToolTip(
            "Recipients for incident, recovery, and SMTP test notifications. Millensys addresses only."
        )
        self.notification_cc = QLineEdit(", ".join(config["email"].get("cc", [])))
        self.notification_cc.setPlaceholderText("Optional CC addresses, comma separated")
        self.daily_report = QCheckBox("Send a daily server health report")
        self.daily_report.setChecked(reports.get("daily_enabled", True))
        self.daily_report_time = QTimeEdit()
        self.daily_report_time.setDisplayFormat("HH:mm")
        daily_time = QTime.fromString(reports.get("daily_time", "08:00"), "HH:mm")
        self.daily_report_time.setTime(daily_time if daily_time.isValid() else QTime(8, 0))
        self.monthly_report = QCheckBox("Send a monthly server health report")
        self.monthly_report.setChecked(reports.get("monthly_enabled", True))
        self.monthly_report_day = QSpinBox()
        self.monthly_report_day.setRange(1, 31)
        self.monthly_report_day.setValue(int(reports.get("monthly_day", 1)))
        self.monthly_report_day.setToolTip(
            "If the month is shorter, the report is sent on its final day."
        )
        self.monthly_report_time = QTimeEdit()
        self.monthly_report_time.setDisplayFormat("HH:mm")
        monthly_time = QTime.fromString(reports.get("monthly_time", "08:15"), "HH:mm")
        self.monthly_report_time.setTime(monthly_time if monthly_time.isValid() else QTime(8, 15))
        self.report_recipients = QLineEdit(", ".join(reports.get("recipients", [])))
        self.report_recipients.setPlaceholderText(
            "Optional Millensys addresses; empty uses notification recipients"
        )
        self.report_summary = QCheckBox("Include event and health summary")
        self.report_summary.setChecked(reports.get("include_summary", True))
        self.report_incidents = QCheckBox("Include incident and recovery details")
        self.report_incidents.setChecked(reports.get("include_incidents", True))
        self.monthly_analytics_attachment = QCheckBox(
            "Attach the full eight-section analytics PDF to the monthly report"
        )
        self.monthly_analytics_attachment.setChecked(
            reports.get("monthly_analytics_attachment", True)
        )
        self.email_max_attempts = QSpinBox()
        self.email_max_attempts.setRange(1, 20)
        self.email_max_attempts.setValue(int(config["email"].get("max_attempts", 5)))
        self.email_max_attempts.setToolTip(
            "Messages are quarantined after this many failed delivery attempts."
        )
        reports_form.addRow("Notification recipients", self.notification_recipients)
        reports_form.addRow("Notification CC", self.notification_cc)
        reports_form.addRow("Daily report", self.daily_report)
        reports_form.addRow("Daily delivery time", self.daily_report_time)
        reports_form.addRow("Monthly report", self.monthly_report)
        reports_form.addRow("Monthly day", self.monthly_report_day)
        reports_form.addRow("Monthly delivery time", self.monthly_report_time)
        reports_form.addRow("Report recipients", self.report_recipients)
        reports_form.addRow("Report sections", self.report_summary)
        reports_form.addRow("", self.report_incidents)
        reports_form.addRow("Monthly client report", self.monthly_analytics_attachment)
        reports_form.addRow("Maximum email attempts", self.email_max_attempts)
        test_email = QPushButton("Save and send SMTP test email")
        test_email.clicked.connect(self.test_smtp)
        reports_form.addRow("SMTP validation", test_email)
        tabs.addTab(reports_tab, "Reports & Email")

        service_tab = QWidget()
        service_layout = QVBoxLayout(service_tab)
        service_layout.setContentsMargins(22, 24, 22, 24)
        service_card = QFrame()
        service_card.setObjectName("card")
        service_box = QVBoxLayout(service_card)
        self.agent_status = QLabel()
        self.agent_status.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        service_box.addWidget(QLabel("WINDOWS MONITORING SERVICE"))
        service_box.addWidget(self.agent_status)
        service_buttons = QHBoxLayout()
        for label, action in (
            ("Install", "install"),
            ("Start", "start"),
            ("Stop", "stop"),
            ("Remove safely", "remove"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, selected=action: self.manage_agent(selected)
            )
            service_buttons.addWidget(button)
        service_box.addLayout(service_buttons)
        service_layout.addWidget(service_card)
        service_layout.addStretch()
        tabs.addTab(service_tab, "Agent Service")
        layout.addWidget(tabs, 1)

        buttons = QHBoxLayout()
        save = QPushButton("Save configuration")
        save.setObjectName("primary")
        guide = QPushButton("Edit full guided configuration")
        save.clicked.connect(lambda: self.save_settings())
        guide.clicked.connect(self.open_setup_guide)
        buttons.addWidget(save)
        buttons.addWidget(guide)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh_agent_status()
        return page

    def license_page(self) -> QWidget:
        page, layout = self.page_shell(
            "License", "Machine-bound Millensys activation and expiration status."
        )
        card = QFrame()
        card.setObjectName("card")
        box = QFormLayout(card)
        box.setContentsMargins(22, 22, 22, 22)
        self.license_state = QLabel()
        self.license_state.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        self.license_client = QLabel()
        self.license_operator = QLabel()
        self.license_server = QLabel()
        self.license_expiry = QLabel()
        self.license_id = QLabel()
        self.license_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addRow("Status", self.license_state)
        box.addRow("Licensed client", self.license_client)
        box.addRow("Millensys operator", self.license_operator)
        box.addRow("Licensed server", self.license_server)
        box.addRow("Expiration date", self.license_expiry)
        box.addRow("License ID", self.license_id)
        layout.addWidget(card)
        actions = QHBoxLayout()
        manage = QPushButton("Renew or replace license…")
        manage.setObjectName("primary")
        manage.clicked.connect(self.manage_license)
        actions.addWidget(manage)
        actions.addStretch()
        layout.addLayout(actions)
        note = QLabel(
            "Offline renewal: create a request, have Millensys Support issue a new signed license, "
            "then import it. Configuration and monitoring history remain unchanged."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#98a6aa")
        layout.addWidget(note)
        layout.addStretch()
        self.refresh_license()
        return page

    def refresh_license(self) -> None:
        if not hasattr(self, "license_state"):
            return
        status = current_license()
        payload = status.payload
        self.license_state.setText("Active" if status.valid else status.message)
        self.license_state.setObjectName("statusGood" if status.valid else "statusBad")
        self.license_state.style().unpolish(self.license_state)
        self.license_state.style().polish(self.license_state)
        self.license_client.setText(str(payload.get("client_name", "—")))
        self.license_operator.setText(str(payload.get("operator_email", "—")))
        self.license_server.setText(str(payload.get("server_name", "—")))
        self.license_expiry.setText(str(payload.get("expires_on", "—")))
        self.license_id.setText(str(payload.get("license_id", "—")))

    def manage_license(self) -> None:
        ActivationDialog(self.store, self).exec()
        self.refresh_license()

    def table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setDefaultSectionSize(30)
        return table

    def show_page(self, name: str) -> None:
        self.stack.setCurrentWidget(self.pages[name])
        for page_name, button in self.nav_buttons.items():
            button.setChecked(page_name == name)
        self.refresh_visible()

    def refresh_visible(self) -> None:
        self.refresh_heartbeat()
        current = self.stack.currentWidget()
        if current is self.pages["Overview"]:
            self.refresh_dashboard()
        elif current is self.pages["Windows Services"]:
            self.refresh_target_inventory(
                "service", self.service_table, self.service_summary
            )
        elif current is self.pages["IIS Pools"]:
            self.refresh_target_inventory("pool", self.pool_table, self.pool_summary)
        elif current is self.pages["Disks"]:
            self.refresh_disks()
        elif current is self.pages["Performance"]:
            self.refresh_performance()
        elif current is self.pages["SQL Operations"]:
            self.refresh_sql_operations()
        elif current is self.pages["Assurance"]:
            self.refresh_assurance_rules()
        elif current is self.pages["Reports"]:
            self.refresh_analytics_report()
        elif current is self.pages["Events & Logs"]:
            self.refresh_events()
        elif current is self.pages["License"]:
            self.refresh_license()

    def refresh_dashboard(self) -> None:
        metrics = performance()
        self.cpu_card.findChild(QLabel, "value").setText(f"{metrics['cpu']:.0f}%")
        self.memory_card.findChild(QLabel, "value").setText(f"{metrics['memory']:.0f}%")
        for card, value in ((self.cpu_card, metrics["cpu"]), (self.memory_card, metrics["memory"])):
            progress = card.findChild(QProgressBar, "progress")
            progress.setValue(int(value))
            progress.show()
        active = self.store.active_alerts()
        score_incidents = self.store.open_score_incidents()
        assessment = assess_health(self.store)
        config = self.store.get("configuration")
        identity = server_identity()
        client = config.get("client", {})
        site = client.get("site") or "Site not configured"
        self.dashboard_identity.setText(
            f"{client.get('name') or 'Client not configured'}  ·  {identity.server_name}  ·  "
            f"{site}  ·  v{__version__}"
        )
        heartbeat = self.store.get("agent_heartbeat", {})
        heartbeat_age = time.time() - float(heartbeat.get("timestamp", 0))
        if heartbeat.get("state") == "running" and heartbeat_age < 120:
            self.dashboard_agent.setText(f"● Monitoring active  ·  last check {int(heartbeat_age)}s ago")
            self.dashboard_agent.setObjectName("statusGood")
        else:
            self.dashboard_agent.setText("● Monitoring data may be stale · service not reporting")
            self.dashboard_agent.setObjectName("statusBad")
        self.dashboard_agent.style().unpolish(self.dashboard_agent)
        self.dashboard_agent.style().polish(self.dashboard_agent)
        self.health_gauge.set_score(assessment.score)
        self.health_status_value.setText(assessment.label)
        health_object = (
            "statusGood"
            if assessment.label == "Healthy"
            else "statusWarn"
            if assessment.label == "Attention"
            else "statusBad"
        )
        self.health_status_value.setObjectName(health_object)
        self.health_status_value.style().unpolish(self.health_status_value)
        self.health_status_value.style().polish(self.health_status_value)
        self.health_explanation.setText(
            assessment.explanation
            + (
                "  Main contributors: " + " · ".join(assessment.contributors[:3])
                if assessment.contributors
                else "  All monitored areas currently report healthy."
            )
        )
        self.alert_card.findChild(QLabel, "value").setText(str(len(score_incidents)))
        services = self.store.targets("service")
        pools = self.store.targets("pool")
        drives = disks()
        lowest_disk = min(drives, key=lambda item: item["free_percent"], default=None)
        self.disk_card.findChild(QLabel, "value").setText(
            f"{lowest_disk['free_percent']:.0f}% · {lowest_disk['name']}"
            if lowest_disk
            else "No data"
        )
        disk_progress = self.disk_card.findChild(QProgressBar, "progress")
        disk_progress.setValue(int(lowest_disk["free_percent"]) if lowest_disk else 0)
        disk_progress.show()
        self.service_card.findChild(QLabel, "value").setText(
            f"{sum(bool(row['enabled']) for row in services)}/{len(services)} protected"
        )
        self.pool_card.findChild(QLabel, "value").setText(
            f"{sum(bool(row['enabled']) for row in pools)}/{len(pools)} protected"
        )
        for card, rows in ((self.service_card, services), (self.pool_card, pools)):
            progress = card.findChild(QProgressBar, "progress")
            progress.setValue(
                int(100 * sum(bool(row["enabled"]) for row in rows) / len(rows)) if rows else 0
            )
            progress.show()
        category_counts = {
            category: sum(row["category"] == category for row in active)
            for category in ("service", "pool", "disk", "performance")
        }
        enabled_services = sum(bool(row["enabled"]) for row in services)
        enabled_pools = sum(bool(row["enabled"]) for row in pools)
        api_count = 0
        for row in services + pools:
            if json.loads(row["config"] or "{}").get("api_check", {}).get("enabled"):
                api_count += 1
        coverage_values = (
            (self.service_coverage, enabled_services, category_counts["service"]),
            (self.pool_coverage, enabled_pools, category_counts["pool"]),
            (self.disk_coverage, len(drives), category_counts["disk"]),
            (self.api_coverage, api_count, sum(row["condition"] == "api_unhealthy" for row in active)),
        )
        for (label, progress), total, issues in coverage_values:
            healthy = max(0, total - issues)
            label.setText(f"{healthy}/{total} healthy" if total else "Not configured")
            progress.setValue(int(100 * healthy / total) if total else 0)
        self.dashboard_issues.clear()
        if not active:
            self.add_dashboard_item(
                self.dashboard_issues,
                "✓  All monitored items are healthy",
                "No active incident is reducing the server health score.",
                color="#58d68d",
            )
        for row in active:
            since = display_server_time(self.store, row["first_seen"] or row["last_seen"])
            severity_icon = "●" if row["severity"] in {"critical", "error"} else "▲"
            self.add_dashboard_item(
                self.dashboard_issues,
                f"{severity_icon}  {str(row['severity']).upper()}   {row['target']}",
                f"{row['message'] or row['condition']} · since {since} · "
                f"{row['occurrences']} occurrence(s) · double-click to inspect",
                target=str(row["target"]),
                color="#ff6577"
                if row["severity"] in {"critical", "error"}
                else "#f2b84b",
            )
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        self.dashboard_actions.clear()
        action_rows = [
            row
            for row in self.store.recent_events(100)
            if row["category"]
            in {"action", "api_action", "scheduled_action", "recovery"}
        ][:10]
        if not action_rows:
            self.add_dashboard_item(
                self.dashboard_actions,
                "No recent automatic actions",
                "MiMonitoringService has not needed to intervene recently.",
                color="#58d68d",
            )
        for row in action_rows:
            icon = "✓" if row["severity"] in {"info", "recovery"} else "!"
            self.add_dashboard_item(
                self.dashboard_actions,
                f"{icon}  {row['target']}  ·  {row['created_at'][11:19]}",
                str(row["message"]),
                color="#58d68d" if icon == "✓" else "#ff6577",
            )

    def open_issue_item(self, item: QListWidgetItem) -> None:
        target = item.data(Qt.ItemDataRole.UserRole)
        if target:
            TargetHistoryDialog(self.store, str(target), self).exec()

    def refresh_disks(self) -> None:
        items = disks()
        selected = {row["target_key"]: row for row in self.store.targets("disk")}
        monitor_all = self.store.get("configuration")["disk"].get("monitor_all", True)
        self.disk_table.setRowCount(len(items))
        for row, item in enumerate(items):
            target = selected.get(item["key"])
            monitor = QCheckBox()
            monitor.setChecked(monitor_all or bool(target and target["enabled"]))
            monitor.setEnabled(not monitor_all)
            monitor.setToolTip(
                "All drives are monitored" if monitor_all else "Enable or disable this drive"
            )
            monitor.stateChanged.connect(
                lambda state, disk=item: self.toggle_disk_monitoring(disk, state)
            )
            self.disk_table.setCellWidget(row, 0, monitor)
            values = [
                item["name"],
                f"{item['free_mb']:,}",
                f"{item['free_percent']:.1f}%",
                "Healthy" if item["free_percent"] >= 15 else "Low space",
            ]
            for col, value in enumerate(values, start=1):
                self.disk_table.setItem(row, col, QTableWidgetItem(value))

    def refresh_performance(self) -> None:
        metrics = performance()
        consumers = performance_consumers(15)
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        samples = self.store.health_samples(since)
        peaks = self.store.performance_peaks(since, 20)
        self.performance_cpu_card.findChild(QLabel, "value").setText(
            f"{metrics['cpu']:.1f}%"
        )
        self.performance_memory_card.findChild(QLabel, "value").setText(
            f"{metrics['memory']:.1f}%"
        )
        for card, value in (
            (self.performance_cpu_card, metrics["cpu"]),
            (self.performance_memory_card, metrics["memory"]),
        ):
            progress = card.findChild(QProgressBar, "progress")
            progress.setValue(int(value))
            progress.show()
        peak = max(samples, key=lambda row: max(row["cpu"], row["memory"]), default=None)
        self.performance_peak_card.findChild(QLabel, "value").setText(
            f"{max(peak['cpu'], peak['memory']):.0f}%"
            if peak
            else "Collecting"
        )
        lead = consumers[0] if consumers else None
        self.performance_cause_card.findChild(QLabel, "value").setText(
            lead["name"][:24] if lead else "No attribution"
        )
        self.performance_root_cause.setText(
            f"Current likely cause: {lead['kind']} · {lead['name']} · CPU {lead['cpu']:.1f}% · "
            f"Memory {lead['memory_mb']:.1f} MB"
            if lead
            else "No process attribution is currently available."
        )
        self.performance_trend.set_samples(samples)
        self.performance_consumers_table.setRowCount(len(consumers))
        for row_index, consumer in enumerate(consumers):
            values = (
                consumer["kind"],
                consumer["name"],
                consumer["pid"],
                f"{consumer['cpu']:.1f}%",
                f"{consumer['memory_mb']:.1f} MB",
            )
            for column, value in enumerate(values):
                self.performance_consumers_table.setItem(
                    row_index, column, QTableWidgetItem(str(value))
                )
        self.performance_peaks_table.setRowCount(len(peaks))
        for row_index, peak_row in enumerate(peaks):
            values = (
                display_server_time(self.store, peak_row["created_at"]),
                f"{peak_row['cpu']:.1f}%",
                f"{peak_row['memory']:.1f}%",
                peak_row["root_cause"] or "No attribution available",
            )
            for column, value in enumerate(values):
                self.performance_peaks_table.setItem(
                    row_index, column, QTableWidgetItem(str(value))
                )
        profiles = [item for item in self.store.get("configuration").get("sql_monitors", []) if item.get("enabled", True)]
        selected = self.sql_performance_profile.currentData()
        self.sql_performance_profile.blockSignals(True); self.sql_performance_profile.clear()
        for profile in profiles:
            self.sql_performance_profile.addItem(str(profile.get("name") or profile.get("server")), str(profile.get("id") or profile.get("server")))
        index = self.sql_performance_profile.findData(selected); self.sql_performance_profile.setCurrentIndex(max(0, index)); self.sql_performance_profile.blockSignals(False)
        profile_id = str(self.sql_performance_profile.currentData() or "")
        sql_profile_state = self.store.get("sql_runtime", {}).get(profile_id, {})
        sql_runtime = sql_profile_state.get("performance", {})
        selected_profile = next((item for item in profiles if str(item.get("id") or item.get("server")) == profile_id), {})
        self.sql_kill_button.setEnabled(bool(selected_profile.get("allow_session_termination", False)))
        self.sql_kill_button.setToolTip("" if self.sql_kill_button.isEnabled() else "Enable confirmed session termination in this SQL monitoring profile.")
        sql_samples = self.store.sql_performance_samples(since, profile_id) if profile_id else []
        self.sql_performance_trend.set_samples(sql_samples)
        cpu = sql_runtime.get("sql_cpu_percent"); active = sql_runtime.get("active_sessions")
        self.sql_cpu_card.findChild(QLabel, "value").setText("No data" if cpu is None else f"{float(cpu):.1f}%")
        self.sql_sessions_card.findChild(QLabel, "value").setText("No data" if active is None else str(active))
        self.sql_blocking_card.findChild(QLabel, "value").setText(f"{int(sql_runtime.get('blocked_sessions', 0))} · {int(sql_runtime.get('longest_blocked_seconds', 0))}s")
        self.sql_grants_card.findChild(QLabel, "value").setText(f"{int(sql_runtime.get('memory_grants_pending', 0))} pending")
        comparison = sql_runtime.get("assessment", {}).get("period_comparison", {})
        error = sql_runtime.get("error") or sql_runtime.get("query_evidence_error")
        if error:
            freshness = f"SQL performance evidence is partially unavailable: {error}"
        elif sql_runtime.get("checked_at"):
            hour_p95 = comparison.get("same_hour", {}).get("p95")
            day_p95 = comparison.get("same_weekday", {}).get("p95")
            freshness = (f"Latest sample: {display_server_time(self.store, sql_runtime['checked_at'])} · {comparison.get('weekday', 'Local day')} at "
                         f"{comparison.get('local_hour', 0):02d}:00 baseline · same-hour CPU P95 "
                         f"{'needs history' if hour_p95 is None else f'{hour_p95:.1f}%'} · same-weekday P95 "
                         f"{'needs history' if day_p95 is None else f'{day_p95:.1f}%'}")
        else:
            freshness = "No SQL performance sample yet. Check the profile permission and collection interval."
        self.sql_performance_freshness.setText(freshness)
        session_state = sql_profile_state.get("sessions", {})
        session_rows = list(session_state.get("sessions", []))
        selected_filter = self.sql_session_filter.currentText()
        needle = self.sql_session_search.text().strip().lower()
        filtered_sessions = []
        for item in session_rows:
            if selected_filter == "Blocked" and not item.get("blocking_session_id"):
                continue
            if selected_filter == "Blockers" and not item.get("is_blocker"):
                continue
            if selected_filter not in {"All sessions", "Blocked", "Blockers"} and str(item.get("status", "")).lower() != selected_filter.lower():
                continue
            searchable = " ".join(
                str(item.get(key, ""))
                for key in ("session_id", "login_name", "host_name", "application_name", "database_name", "source_type", "source_name", "query_text")
            ).lower()
            if needle and needle not in searchable:
                continue
            filtered_sessions.append(item)
        self.sql_sessions_table.setRowCount(len(filtered_sessions))
        checked_at = session_state.get("checked_at")
        session_error = session_state.get("error")
        if session_error:
            self.sql_session_freshness.setText(f"Session inventory unavailable: {session_error}")
        elif checked_at:
            self.sql_session_freshness.setText(
                f"Latest interval: {display_server_time(self.store, checked_at)} · {len(session_rows)} user session(s) · "
                f"{int(session_state.get('blocking_count', 0))} blocked · "
                f"{int(session_state.get('blocker_count', 0))} blocker(s)"
            )
        else:
            self.sql_session_freshness.setText("No session inventory has been collected yet.")
        for row, item in enumerate(filtered_sessions):
            wait = str(item.get("wait_type") or "—")
            if item.get("wait_ms"):
                wait += f" · {int(item['wait_ms']) / 1000:,.1f}s"
            transaction = f"{int(item.get('open_transactions', 0))} open"
            if item.get("transaction_age_seconds"):
                transaction += f" · {int(item['transaction_age_seconds']) / 60:,.1f}m"
            source = f"{item.get('source_type') or 'Unknown'} · {item.get('source_name') or 'Unknown'}"
            query_text = str(item.get("query_text") or "No active request text")
            preview = query_text if len(query_text) <= 160 else query_text[:157] + "..."
            values = (
                item.get("session_id"), item.get("session_type"), item.get("status"),
                item.get("blocking_session_id") or "—", item.get("database_name") or "—",
                item.get("login_name") or "—", item.get("host_name") or "—",
                item.get("application_name") or "—", wait, transaction, source, preview,
            )
            tooltip = (
                f"SPID {item.get('session_id')} · {item.get('session_type')} · {item.get('status')}\n"
                f"Blocked by: {item.get('blocking_session_id') or 'No'}\nDatabase: {item.get('database_name') or 'Unknown'}\n"
                f"Login: {item.get('login_name') or 'Unknown'}\nHost: {item.get('host_name') or 'Unknown'}\n"
                f"Application: {item.get('application_name') or 'Unknown'}\nSource: {source}\n\n"
                f"Normalized query content (literal values redacted):\n{query_text}"
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setToolTip(tooltip)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, int(item.get("session_id", 0)))
                self.sql_sessions_table.setItem(row, column, cell)
        io_rows = [(f"{item.get('database')} / {item.get('file')}",
                    "—" if item.get("read_latency_ms") is None else f"{item['read_latency_ms']:.1f} ms",
                    "—" if item.get("write_latency_ms") is None else f"{item['write_latency_ms']:.1f} ms",
                    f"{item.get('read_mb_delta', 0):,.1f}", f"{item.get('write_mb_delta', 0):,.1f} MB")
                   for item in sql_runtime.get("io", [])]
        io_rows += [(f"Wait · {item.get('category')}", "—", "—", "—",
                     f"{item.get('wait_ms', 0):,.0f} ms · {item.get('top_wait')}") for item in sql_runtime.get("waits", [])]
        self.sql_io_wait_table.setRowCount(len(io_rows))
        self.sql_io_wait_table.setVisible(bool(io_rows)); self.sql_io_empty.setVisible(not io_rows)
        for row, values in enumerate(io_rows):
            for column, value in enumerate(values): self.sql_io_wait_table.setItem(row, column, QTableWidgetItem(str(value)))
        query_rows = [dict(row) for row in self.store.sql_blocking_evidence(since, profile_id, 500)] if profile_id else []
        self.sql_query_table.setRowCount(len(query_rows))
        self.sql_query_table.setVisible(bool(query_rows)); self.sql_query_empty.setVisible(not query_rows)
        for row, item in enumerate(query_rows):
            source = f"{item.get('source_type') or 'Unknown'} · {item.get('source_name') or 'Unknown'}"
            query_text = item.get("query_text") or "No active request text was exposed"
            preview = query_text if len(query_text) <= 160 else query_text[:157] + "..."
            wait = str(item.get("status") or "Unknown")
            if item.get("wait_type"): wait += f" · {item['wait_type']}"
            if item.get("wait_ms"): wait += f" · {int(item['wait_ms']) / 1000:,.1f}s"
            role = "Blocker" if item.get("is_blocker") else "Blocked"
            values = (display_server_time(self.store, item.get("collected_at")), item.get("session_id"), role, item.get("blocking_session_id") or "—", wait, item.get("database_name") or "—", source, preview)
            tooltip = (f"SPID {item.get('session_id')} · {role}\nBlocked by: {item.get('blocking_session_id') or 'No'}\n"
                       f"Login: {item.get('login_name') or 'Unknown'}\nHost: {item.get('host_name') or 'Unknown'}\n"
                       f"Application: {item.get('application_name') or 'Unknown'}\nSource: {source}\n\n"
                       f"Normalized query content (literal values redacted):\n{query_text}")
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setToolTip(tooltip)
                self.sql_query_table.setItem(row, column, cell)
        sql_changes = [
            dict(row)
            for row in self.store.component_changes(since=since, component_type="SQL Server")
            if not profile_id or str(row["item_key"]) == f"sql:{profile_id}"
        ]
        self.sql_changes_table.setRowCount(len(sql_changes))
        self.sql_changes_table.setVisible(bool(sql_changes))
        self.sql_changes_empty.setVisible(not sql_changes)
        for row, change in enumerate(sql_changes):
            setting = str(change["property_name"])
            scope = "Database" if setting.startswith("database/") else "Server"
            values = (
                display_server_time(self.store, change["detected_at"]),
                scope,
                setting.split("/", 2)[-1],
                str(change["old_value"]),
                str(change["new_value"]),
                str(change["status"]).title(),
            )
            for column, value in enumerate(values):
                self.sql_changes_table.setItem(row, column, QTableWidgetItem(str(value)))
        deadlocks = []
        for event in self.store.filtered_events(limit=500, since=since):
            if str(event["category"]) != "sql-deadlock":
                continue
            try:
                details = json.loads(str(event["details"] or "{}"))
            except json.JSONDecodeError:
                details = {}
            if profile_id and str(details.get("profile_id") or "") != profile_id:
                continue
            deadlocks.append((dict(event), details))
        self.sql_deadlock_table.setRowCount(len(deadlocks))
        self.sql_deadlock_table.setVisible(bool(deadlocks))
        self.sql_deadlock_empty.setVisible(not deadlocks)
        for row, (event, details) in enumerate(deadlocks):
            victim = details.get("victim") or {}
            processes = details.get("processes") or []
            source = str(victim.get("source") or "Unknown source")
            if victim.get("application"):
                source += f" · {victim['application']}"
            values = (
                display_server_time(self.store, details.get("occurred_at") or event["created_at"]),
                f"SPID {victim.get('spid') or 'unknown'}",
                f"Database ID {victim.get('database_id') or 'unknown'}",
                source,
                str(details.get("likely_cause") or event["message"]),
                str(details.get("evidence_id") or "—"),
            )
            tooltip = "\n\n".join(
                f"SPID {item.get('spid') or 'unknown'} · {item.get('source') or 'Unknown'}\n"
                f"{item.get('query') or 'Query content unavailable'}"
                for item in processes
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value)); cell.setToolTip(tooltip)
                self.sql_deadlock_table.setItem(row, column, cell)

    def toggle_monitor_all_disks(self, state: int) -> None:
        config = self.store.get("configuration")
        config["disk"]["monitor_all"] = bool(state)
        self.store.set("configuration", config)
        self.store.add_event(
            "info",
            "configuration",
            "disks",
            "All-drive monitoring enabled" if state else "Individual drive selection enabled",
        )
        self.refresh_disks()

    def toggle_disk_monitoring(self, disk: dict, state: int) -> None:
        if self.store.get("configuration")["disk"].get("monitor_all", True):
            return
        if self.store.target("disk", disk["key"]) is None:
            self.store.upsert_target("disk", disk["key"], disk["name"], {})
        self.store.set_target_enabled("disk", disk["key"], bool(state))
        self.store.add_event(
            "info",
            "configuration",
            disk["key"],
            f"Disk monitoring {'enabled' if state else 'disabled'}",
        )

    def fill_events(self, table: QTableWidget, limit: int) -> None:
        rows = self.store.recent_events(limit)
        self.populate_events(table, rows)

    def populate_events(self, table: QTableWidget, rows: list) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col, value in enumerate(
                (
                    display_server_time(self.store, row["created_at"]),
                    row["severity"],
                    row["category"],
                    row["target"],
                    row["message"],
                )
            ):
                item = QTableWidgetItem(str(value))
                if row["severity"] in ("critical", "error"):
                    item.setForeground(QColor("#ff6577"))
                elif row["severity"] == "warning":
                    item.setForeground(QColor("#f2b84b"))
                elif row["severity"] == "recovery":
                    item.setForeground(QColor("#58d68d"))
                table.setItem(row_index, col, item)

    def refresh_events(self) -> None:
        if not hasattr(self, "event_table"):
            return
        rows = self.store.filtered_events(
            1000,
            self.event_severity.currentText(),
            self.event_search.text().strip(),
            self._event_since(),
        )
        self.populate_events(self.event_table, rows)

    def export_events(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export audit events", "guardian-events.csv", "CSV files (*.csv)"
        )
        if not filename:
            return
        rows = self.store.filtered_events(
            100000,
            self.event_severity.currentText(),
            self.event_search.text().strip(),
            self._event_since(),
        )
        try:
            with Path(filename).open("w", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output)
                writer.writerow(["Time", "Severity", "Category", "Target", "Message", "Details"])
                writer.writerows(
                    (
                        row["created_at"],
                        row["severity"],
                        row["category"],
                        row["target"],
                        row["message"],
                        row["details"],
                    )
                    for row in rows
                )
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Exported {len(rows)} events.")

    def _event_since(self) -> str | None:
        periods = {
            "Last 24 hours": timedelta(hours=24),
            "Last 7 days": timedelta(days=7),
            "Last 30 days": timedelta(days=30),
        }
        duration = periods.get(self.event_period.currentText())
        return (datetime.now(UTC) - duration).isoformat(timespec="seconds") if duration else None

    def open_issue_history(self, row: int, column: int) -> None:
        item = self.dashboard_issues.item(row, 1)
        if item:
            TargetHistoryDialog(self.store, item.text(), self).exec()

    def discover_targets(self, kind: str, table: QTableWidget) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            items = windows_services() if kind == "service" else iis_pools()
        finally:
            QApplication.restoreOverrideCursor()
        if kind == "pool" and not items:
            QMessageBox.warning(
                self,
                "IIS discovery unavailable",
                "No IIS application pools were returned. Confirm IIS is installed and run MiMonitoringService as administrator.",
            )
        saved = {
            row["target_key"]: {
                "key": row["target_key"], "name": row["display_name"], "status": "Unavailable"
            }
            for row in self.store.targets(kind)
        }
        saved.update({item["key"]: item for item in items})
        self.populate_target_table(kind, table, list(saved.values()))

    def populate_target_table(
        self, kind: str, table: QTableWidget, items: list[dict]
    ) -> None:
        selected = {row["target_key"]: row for row in self.store.targets(kind)}
        table.setRowCount(len(items))
        for row, item in enumerate(sorted(items, key=lambda value: value["name"].lower())):
            target_row = selected.get(item["key"])
            check = QCheckBox()
            check.setChecked(target_row is not None)
            check.setToolTip("Add or remove this item from Guardian")
            check.stateChanged.connect(
                lambda state, x=item, t=table: self.select_target(kind, x, state, t)
            )
            table.setCellWidget(row, 0, check)
            enabled = QCheckBox()
            enabled.setChecked(bool(target_row["enabled"]) if target_row else False)
            enabled.setEnabled(target_row is not None)
            enabled.setToolTip(
                "Temporarily enable or disable all monitoring, actions, and alerts for this item"
            )
            enabled.stateChanged.connect(
                lambda state, key=item["key"]: self.toggle_monitoring(kind, key, state)
            )
            table.setCellWidget(row, 1, enabled)
            table.setItem(row, 2, QTableWidgetItem(item["name"]))
            table.setItem(row, 3, QTableWidgetItem(item["status"]))
            configure = QPushButton("Configure")
            configure.setEnabled(target_row is not None)
            configure.clicked.connect(
                lambda checked=False, key=item["key"]: self.configure_target(kind, key)
            )
            table.setCellWidget(row, 4, configure)
            control = QComboBox()
            control.addItems(["Action…", "Start", "Stop"] + (["Recycle"] if kind == "pool" else []))
            control.currentTextChanged.connect(
                lambda action, key=item["key"]: self.control_target(kind, key, action)
            )
            table.setCellWidget(row, 5, control)

    def select_target(self, kind: str, item: dict, state: int, table: QTableWidget) -> None:
        if state:
            self.store.upsert_target(
                kind, item["key"], item["name"], {"auto_restart": True, "retry_count": 3}
            )
            self.store.add_event(
                "info", "configuration", item["key"], f"{kind.title()} selected for monitoring"
            )
        else:
            with self.store.connect() as db:
                db.execute("DELETE FROM targets WHERE kind=? AND target_key=?", (kind, item["key"]))
            self.store.deactivate_target_alerts(kind, item["key"])
            self.store.add_event(
                "info", "configuration", item["key"], f"{kind.title()} removed from monitoring"
            )
        self.discover_targets(kind, table)

    def toggle_monitoring(self, kind: str, key: str, state: int) -> None:
        if self.store.target(kind, key) is None:
            return
        enabled = bool(state)
        self.store.set_target_enabled(kind, key, enabled)
        self.store.add_event(
            "info", "configuration", key, f"Monitoring {'enabled' if enabled else 'disabled'}"
        )

    def configure_target(self, kind: str, key: str) -> None:
        try:
            TargetConfigDialog(self.store, kind, key, self).exec()
        except ValueError as exc:
            QMessageBox.warning(self, "Target unavailable", str(exc))

    def control_target(self, kind: str, key: str, action: str) -> None:
        action = action.lower()
        if action not in {"start", "stop", "recycle"}:
            return
        if (
            QMessageBox.question(
                self,
                "Confirm server action",
                f"{action.title()} {key}? This action will be recorded.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        ok, message = service_action(key, action) if kind == "service" else pool_action(key, action)
        self.store.add_event(
            "info" if ok else "error",
            "manual_action",
            key,
            f"{action.title()} requested",
            result=message,
        )
        QMessageBox.information(self, "Action result", message or ("Completed" if ok else "Failed"))

    def filter_table(self, table: QTableWidget, text: str) -> None:
        query = text.casefold()
        for row in range(table.rowCount()):
            item = table.item(row, 2)
            table.setRowHidden(row, bool(query and item and query not in item.text().casefold()))

    def save_settings(self, show_confirmation: bool = True) -> bool:
        timezone_name = self.timezone.text().strip() or "SERVER_LOCAL"
        try:
            if timezone_name.upper() not in {"SERVER_LOCAL", "LOCAL"}:
                ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            QMessageBox.warning(
                self,
                "Invalid timezone",
                "Enter SERVER_LOCAL or a valid IANA timezone such as Africa/Cairo.",
            )
            return False
        config = self.store.get("configuration")
        notification_entries = _email_entries(self.notification_recipients.text())
        notification_result = validate_recipients(
            notification_entries, config["registration"]
        )
        if not notification_result.valid:
            QMessageBox.warning(
                self, "Invalid notification recipient", notification_result.message
            )
            return False
        report_entries = _email_entries(self.report_recipients.text())
        report_result = validate_recipients(
            report_entries, config["registration"], required=False
        )
        if not report_result.valid:
            QMessageBox.warning(self, "Invalid report recipient", report_result.message)
            return False
        config["performance"]["cpu_percent"] = self.cpu_threshold.value()
        config["performance"]["memory_percent"] = self.memory_threshold.value()
        config["performance"]["sustain_seconds"] = self.sustain_seconds.value()
        config["disk"]["minimum_free_percent"] = self.disk_threshold.value()
        config["agent"]["debug"] = self.debug.isChecked()
        config["agent"]["automatic_actions"] = self.automatic_actions.isChecked()
        config["agent"]["timezone"] = timezone_name
        config["agent"]["close_to_tray"] = self.close_to_tray.isChecked()
        config["agent"]["start_minimized"] = self.start_minimized.isChecked()
        config["diagnostics"]["retention_days"] = self.retention_days.value()
        config["diagnostics"]["max_events"] = self.maximum_events.value()
        config["diagnostics"]["trend_hours"] = self.trend_hours.value()
        reports = config["reports"]
        reports["daily_enabled"] = self.daily_report.isChecked()
        reports["daily_time"] = self.daily_report_time.time().toString("HH:mm")
        reports["monthly_enabled"] = self.monthly_report.isChecked()
        reports["monthly_day"] = self.monthly_report_day.value()
        reports["monthly_time"] = self.monthly_report_time.time().toString("HH:mm")
        reports["recipients"] = _emails(self.report_recipients.text())
        reports["include_summary"] = self.report_summary.isChecked()
        reports["include_incidents"] = self.report_incidents.isChecked()
        reports["monthly_analytics_attachment"] = self.monthly_analytics_attachment.isChecked()
        config["email"]["recipients"] = _emails(self.notification_recipients.text())
        config["email"]["cc"] = _emails(self.notification_cc.text())
        config["email"]["max_attempts"] = self.email_max_attempts.value()
        self.store.set("configuration", config)
        self.store.add_event("info", "configuration", "settings", "Monitoring settings updated")
        if show_confirmation:
            QMessageBox.information(self, "Saved", "Configuration saved successfully.")
        return True

    def open_setup_guide(self) -> None:
        if SetupWizard(self.store, self).exec() != QDialog.DialogCode.Accepted:
            return
        config = self.store.get("configuration")
        self.cpu_threshold.setValue(config["performance"]["cpu_percent"])
        self.memory_threshold.setValue(config["performance"]["memory_percent"])
        self.sustain_seconds.setValue(config["performance"]["sustain_seconds"])
        self.disk_threshold.setValue(config["disk"]["minimum_free_percent"])
        self.automatic_actions.setChecked(config["agent"]["automatic_actions"])
        self.timezone.setText(config["agent"]["timezone"])
        self.daily_report.setChecked(config["reports"]["daily_enabled"])
        self.monthly_report.setChecked(config["reports"]["monthly_enabled"])
        self.notification_recipients.setText(
            ", ".join(config["email"]["recipients"])
        )
        self.notification_cc.setText(", ".join(config["email"]["cc"]))
        self.report_recipients.setText(", ".join(config["reports"]["recipients"]))
        self.monitor_all_disks.setChecked(config["disk"]["monitor_all"])
        self.refresh_agent_status()

    def test_smtp(self) -> None:
        if not self.save_settings(show_confirmation=False):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = Notifier(self.store).send_test()
        finally:
            QApplication.restoreOverrideCursor()
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "SMTP test" if ok else "SMTP test failed", message
        )

    def refresh_agent_status(self) -> None:
        if hasattr(self, "agent_status"):
            self.agent_status.setText(f"Status: {agent_service_status()}")

    def refresh_heartbeat(self) -> None:
        heartbeat = self.store.get("agent_heartbeat", {})
        state = heartbeat.get("state")
        age = time.time() - float(heartbeat.get("timestamp", 0))
        poll_seconds = int(self.store.get("configuration")["agent"].get("poll_seconds", 30))
        if state == "running" and age <= max(90, poll_seconds * 3):
            self.agent_console_status.setText("● Monitoring agent healthy")
            self.agent_console_status.setStyleSheet("color:#58d68d")
        elif state == "running":
            self.agent_console_status.setText("● Agent heartbeat is stale")
            self.agent_console_status.setStyleSheet("color:#f2b84b")
        elif state == "stopped":
            self.agent_console_status.setText("● Monitoring agent stopped")
            self.agent_console_status.setStyleSheet("color:#ff6577")
        else:
            self.agent_console_status.setText("● Agent has not reported yet")
            self.agent_console_status.setStyleSheet("color:#98a6aa")

    def manage_agent(self, action: str) -> None:
        if action in {"install", "start"}:
            license_status = current_license()
            if not license_status.valid:
                QMessageBox.warning(self, "Activation required", license_status.message)
                return
        if (
            action in {"stop", "remove"}
            and QMessageBox.question(
                self, "Confirm service change", f"{action.title()} the monitoring service?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        ok, message = manage_agent_service(action)
        self.store.add_event(
            "info" if ok else "error",
            "service_manager",
            "guardian",
            f"Service {action} requested",
            result=message,
        )
        self.refresh_agent_status()
        (QMessageBox.information if ok else QMessageBox.warning)(
            self,
            "Service management",
            message or ("Completed" if ok else "Failed. Run the GUI as administrator."),
        )
