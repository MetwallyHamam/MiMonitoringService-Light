from __future__ import annotations

import json
import secrets
import smtplib
import sys
import time
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from millensys_guardian.licensing import PRODUCT, issue_license, load_signing_key_data
from millensys_guardian.resources import resource_path
from millensys_guardian.security import protect, unprotect
from millensys_guardian.ui import STYLE

OTP_EXPIRY_SECONDS = 60 * 60
OTP_REFERENCE_EMAIL = "m_hammam@millensys.com"


def build_otp_message(
    *,
    sender_name: str,
    sender_email: str,
    recipient: str,
    otp_code: str,
    client_name: str,
    server_name: str,
) -> EmailMessage:
    """Create the branded registration verification email."""
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = recipient
    message["Bcc"] = OTP_REFERENCE_EMAIL
    message["Subject"] = "MiMonitoringService offline license verification code"
    message.set_content(
        "Your MiMonitoringService license verification code is "
        f"{otp_code}. It expires in one hour.\n\n"
        f"Client: {client_name}\nServer: {server_name}"
    )
    safe_code = escape(otp_code)
    safe_client = escape(client_name)
    safe_server = escape(server_name)
    message.add_alternative(
        f"""<!doctype html>
<html><body style="margin:0;background:#f3f6fb;font-family:Segoe UI,Arial,sans-serif;color:#172033">
<table width="100%" role="presentation"><tr><td align="center" style="padding:32px 12px">
<table width="640" role="presentation" style="max-width:640px;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 8px 30px #15213a1f">
<tr><td style="background:#101c35;padding:24px 30px;color:#fff"><div style="font-size:12px;letter-spacing:2px;color:#8ab4ff">MILLENSYS</div><div style="font-size:22px;font-weight:700;margin-top:6px">MiMonitoringService</div></td></tr>
<tr><td style="height:5px;background:#2563eb"></td></tr>
<tr><td style="padding:30px"><h2 style="margin:0 0 16px">Verify your registration</h2>
<p style="font-size:15px;line-height:1.6">Use this verification code to continue issuing the offline license:</p>
<div style="margin:24px 0;padding:18px;text-align:center;background:#eff6ff;border:1px solid #bfdbfe;border-radius:10px;font-size:32px;font-weight:700;letter-spacing:8px;color:#1d4ed8">{safe_code}</div>
<p style="font-size:14px;line-height:1.6"><b>This code expires in one hour.</b></p>
<div style="margin-top:24px;padding:14px;background:#f1f5f9;border-radius:8px;font-size:13px;color:#334155"><b>Client:</b> {safe_client}<br><b>Server:</b> {safe_server}</div>
<p style="margin-top:24px;font-size:13px;color:#64748b">If you did not request this registration, you can ignore this email.</p></td></tr>
<tr><td style="padding:18px 30px;background:#f7f9fc;color:#64748b;font-size:12px">Millensys licensing verification · Automated message</td></tr>
</table></td></tr></table></body></html>""",
        subtype="html",
    )
    return message


def private_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "support-private"
    return Path(__file__).resolve().parents[1] / "support-private"


class LicenseGenerator(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.request: dict | None = None
        self.otp_code = ""
        self.otp_expires_at = 0.0
        self.email_verified = False
        self.request_path = QLineEdit()
        self.request_path.setReadOnly(True)
        self.client = QLabel("No request loaded")
        self.operator = QLabel("—")
        self.server = QLabel("—")
        self.activation_mode = QLabel("—")
        self.fingerprint = QLabel("—")
        self.fingerprint.setWordWrap(True)
        self.expiry = QDateEdit(QDate.currentDate().addYears(1))
        self.expiry.setCalendarPopup(True)
        self.expiry.setDisplayFormat("yyyy-MM-dd")
        self.expiry.setMinimumDate(QDate.currentDate())
        self.smtp_server = QLineEdit("smtp.gmail.com")
        self.smtp_port = QSpinBox()
        self.smtp_port.setRange(1, 65535)
        self.smtp_port.setValue(587)
        self.smtp_username = QLineEdit("millensysnotifications@gmail.com")
        self.smtp_sender = QLineEdit("MiMonitoringService Licensing")
        self.smtp_tls = QCheckBox("Use STARTTLS")
        self.smtp_tls.setChecked(True)
        self.smtp_password = QLineEdit()
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.otp = QLineEdit()
        self.otp.setPlaceholderText("6-digit verification code")
        self.otp.setMaxLength(6)
        self.setWindowTitle("Millensys · MiMonitoringService License Generator")
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)
        title = QLabel("Issue MiMonitoringService license")
        title.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        layout.addWidget(title)
        warning = QLabel(
            "MILLENSYS SUPPORT ONLY · The signing key and SMTP provisioning secret remain outside "
            "the generated customer license and application package."
        )
        warning.setWordWrap(True)
        warning.setObjectName("statusWarn")
        layout.addWidget(warning)
        request_row = QHBoxLayout()
        request_row.addWidget(self.request_path, 1)
        browse = QPushButton("Open request…")
        browse.clicked.connect(self.open_request)
        request_row.addWidget(browse)
        layout.addLayout(request_row)
        form = QFormLayout()
        form.addRow("Product", QLabel(PRODUCT))
        form.addRow("Client", self.client)
        form.addRow("Operator", self.operator)
        form.addRow("Server", self.server)
        form.addRow("Activation mode", self.activation_mode)
        form.addRow("Machine fingerprint", self.fingerprint)
        otp_row = QHBoxLayout()
        otp_row.addWidget(self.otp)
        send_otp = QPushButton("Send OTP")
        send_otp.clicked.connect(self.send_otp)
        verify_otp = QPushButton("Verify")
        verify_otp.clicked.connect(self.verify_otp)
        otp_row.addWidget(send_otp)
        otp_row.addWidget(verify_otp)
        form.addRow("Email verification", otp_row)
        self.otp_status = QLabel("Not verified")
        self.otp_status.setObjectName("statusWarn")
        form.addRow("OTP status", self.otp_status)
        form.addRow("License expiration", self.expiry)
        layout.addLayout(form)
        smtp_title = QLabel("OTP SMTP configuration")
        smtp_title.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        layout.addWidget(smtp_title)
        smtp_form = QFormLayout()
        smtp_form.addRow("SMTP server", self.smtp_server)
        smtp_form.addRow("Port", self.smtp_port)
        smtp_form.addRow("Username", self.smtp_username)
        smtp_form.addRow("Sender name", self.smtp_sender)
        smtp_form.addRow("Security", self.smtp_tls)
        smtp_form.addRow("App password", self.smtp_password)
        save_smtp = QPushButton("Save SMTP configuration securely")
        save_smtp.clicked.connect(lambda: self.save_smtp_config(show_message=True))
        smtp_form.addRow("", save_smtp)
        layout.addLayout(smtp_form)
        layout.addStretch()
        issue = QPushButton("Issue signed license…")
        issue.setObjectName("primary")
        issue.clicked.connect(self.generate)
        layout.addWidget(issue)
        self.load_smtp_config()

    @property
    def smtp_config_path(self) -> Path:
        return private_directory() / "generator-smtp.json"

    def load_smtp_config(self) -> None:
        secret_path = private_directory() / "smtp-app-password.txt"
        try:
            if self.smtp_config_path.exists():
                config = json.loads(self.smtp_config_path.read_text(encoding="utf-8"))
                self.smtp_server.setText(str(config.get("smtp_server", "smtp.gmail.com")))
                self.smtp_port.setValue(int(config.get("smtp_port", 587)))
                self.smtp_username.setText(
                    str(config.get("username", "millensysnotifications@gmail.com"))
                )
                self.smtp_sender.setText(str(config.get("sender_name", "MiMonitoringService")))
                self.smtp_tls.setChecked(bool(config.get("use_tls", True)))
                self.smtp_password.setText(unprotect(str(config.get("password_protected", ""))))
            elif secret_path.exists():
                self.smtp_password.setText(secret_path.read_text(encoding="utf-8").strip())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "SMTP configuration", f"Cannot load SMTP settings: {exc}")

    def smtp_configuration(self) -> dict:
        return {
            "smtp_server": self.smtp_server.text().strip(),
            "smtp_port": self.smtp_port.value(),
            "username": self.smtp_username.text().strip(),
            "sender_name": self.smtp_sender.text().strip() or "MiMonitoringService",
            "use_tls": self.smtp_tls.isChecked(),
        }

    def save_smtp_config(self, *, show_message: bool = False) -> bool:
        if not self.smtp_server.text().strip() or not self.smtp_username.text().strip():
            QMessageBox.warning(self, "SMTP configuration", "SMTP server and username are required.")
            return False
        if not self.smtp_password.text():
            QMessageBox.warning(self, "SMTP configuration", "Enter the SMTP app password.")
            return False
        private_directory().mkdir(parents=True, exist_ok=True)
        config = self.smtp_configuration()
        config["password_protected"] = protect(self.smtp_password.text().replace(" ", ""))
        self.smtp_config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        if show_message:
            QMessageBox.information(
                self,
                "SMTP configuration saved",
                "The app password was protected with Windows DPAPI on this support computer.",
            )
        return True

    def open_request(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open activation request", "", "Activation request (*.mirequest)"
        )
        if not filename:
            return
        try:
            request = json.loads(Path(filename).read_text(encoding="utf-8"))
            if request.get("product") != PRODUCT:
                raise ValueError("Request is for another product")
            if not str(request.get("operator_email", "")).lower().endswith("@millensys.com"):
                raise ValueError("Request operator is not a Millensys email address")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Invalid request", str(exc))
            return
        self.request = request
        self.otp_code = ""
        self.otp_expires_at = 0.0
        self.email_verified = False
        self.otp.clear()
        self.otp_status.setText("Not verified")
        self.request_path.setText(filename)
        self.client.setText(str(request.get("client_name", "")))
        self.operator.setText(str(request.get("operator_email", "")))
        self.server.setText(str(request.get("server_name", "")))
        self.activation_mode.setText(str(request.get("activation_mode", "offline")).title())
        self.fingerprint.setText(str(request.get("machine_fingerprint", "")))

    def send_otp(self) -> None:
        if not self.request:
            QMessageBox.warning(self, "Request required", "Open a .mirequest file first.")
            return
        if not self.save_smtp_config():
            return
        self.otp_code = f"{secrets.randbelow(1_000_000):06d}"
        recipient = str(self.request["operator_email"])
        message = build_otp_message(
            sender_name=self.smtp_sender.text().strip(),
            sender_email=self.smtp_username.text().strip(),
            recipient=recipient,
            otp_code=self.otp_code,
            client_name=str(self.request["client_name"]),
            server_name=str(self.request["server_name"]),
        )
        try:
            with smtplib.SMTP(
                self.smtp_server.text().strip(), self.smtp_port.value(), timeout=20
            ) as smtp:
                if self.smtp_tls.isChecked():
                    smtp.starttls()
                smtp.login(
                    self.smtp_username.text().strip(),
                    self.smtp_password.text().replace(" ", ""),
                )
                smtp.send_message(message)
        except Exception as exc:  # noqa: BLE001
            self.otp_code = ""
            QMessageBox.critical(self, "OTP delivery failed", str(exc))
            return
        self.otp_expires_at = time.monotonic() + OTP_EXPIRY_SECONDS
        self.email_verified = False
        self.otp_status.setText(f"Code sent to {recipient}; expires in one hour")
        QMessageBox.information(self, "OTP sent", f"Verification code sent to {recipient}.")

    def verify_otp(self) -> None:
        if not self.otp_code or time.monotonic() > self.otp_expires_at:
            self.email_verified = False
            QMessageBox.warning(self, "OTP expired", "Send a new verification code.")
            return
        if not secrets.compare_digest(self.otp.text().strip(), self.otp_code):
            self.email_verified = False
            QMessageBox.warning(self, "Incorrect code", "The verification code is incorrect.")
            return
        self.email_verified = True
        self.otp_code = ""
        self.otp_status.setText("Millensys email verified")
        self.otp_status.setObjectName("statusGood")
        self.otp_status.style().unpolish(self.otp_status)
        self.otp_status.style().polish(self.otp_status)

    def generate(self) -> None:
        if not self.request:
            QMessageBox.warning(self, "Request required", "Open a .mirequest file first.")
            return
        if not self.email_verified:
            QMessageBox.warning(
                self,
                "Email verification required",
                "Send and verify the OTP before issuing this offline license.",
            )
            return
        private_dir = private_directory()
        signing_key_path = private_dir / "license-signing-private.dpapi"
        if not signing_key_path.exists():
            QMessageBox.critical(
                self,
                "Support secrets unavailable",
                f"Required private files are missing from:\n{private_dir}",
            )
            return
        if not self.save_smtp_config():
            return
        output_name = f"MiMonitoringService-{self.request['server_name']}.milic"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save signed license", output_name, "Millensys license (*.milic)"
        )
        if not filename:
            return
        try:
            expiry = date(
                self.expiry.date().year(), self.expiry.date().month(), self.expiry.date().day()
            )
            document = issue_license(
                self.request,
                load_signing_key_data(
                    unprotect(signing_key_path.read_text(encoding="utf-8")).encode("ascii")
                ),
                self.smtp_password.text(),
                expiry,
                self.smtp_configuration(),
            )
            Path(filename).write_text(json.dumps(document, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "License generation failed", str(exc))
            return
        QMessageBox.information(
            self,
            "License issued",
            f"Signed license created for {self.request['server_name']} through {expiry.isoformat()}.",
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Millensys License Generator")
    app.setStyleSheet(STYLE)
    icon = resource_path("assets/mimonitoringservice-icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    dialog = LicenseGenerator()
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
