from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RegistrationResult:
    valid: bool
    message: str


def validate_operator(email: str, registration: dict) -> RegistrationResult:
    normalized = email.strip().lower()
    domain = str(registration.get("required_domain", "millensys.com")).lower()
    if not normalized or normalized.count("@") != 1:
        return RegistrationResult(False, "Enter your Millensys business email address.")
    if not normalized.endswith(f"@{domain}"):
        return RegistrationResult(False, f"Access requires an email ending with @{domain}.")
    mode = registration.get("mode", "domain")
    if mode == "allowlist":
        allowed = {str(item).strip().lower() for item in registration.get("registered_users", [])}
        if normalized not in allowed:
            return RegistrationResult(
                False, "This Millensys email is not registered for this tool."
            )
    return RegistrationResult(True, "Millensys operator validated.")


def validate_recipients(
    emails: list[str], registration: dict, *, required: bool = True
) -> RegistrationResult:
    """Validate notification/report recipients against the configured business domain."""
    domain = str(registration.get("required_domain", "millensys.com")).strip().lower()
    normalized = [str(email).strip().lower() for email in emails if str(email).strip()]
    if required and not normalized:
        return RegistrationResult(False, "Enter at least one Millensys recipient.")
    pattern = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    invalid = [
        email
        for email in normalized
        if not pattern.fullmatch(email) or not email.endswith(f"@{domain}")
    ]
    if invalid:
        return RegistrationResult(
            False,
            f"Every recipient must end with @{domain}. Check: {', '.join(invalid)}",
        )
    return RegistrationResult(True, "Millensys recipients validated.")
