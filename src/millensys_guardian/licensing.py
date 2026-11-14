from __future__ import annotations

import base64
import hashlib
import json
import platform
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import __version__
from .license_public_key import LICENSE_PUBLIC_KEY_PEM
from .paths import data_dir
from .security import protect, unprotect
from .system_info import server_identity

PRODUCT = "MiMonitoringService"
LICENSE_FILE = "license.milic"
ACTIVATION_KEY_FILE = "activation-private.dpapi"


@dataclass(frozen=True)
class LicenseStatus:
    valid: bool
    message: str
    payload: dict


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def machine_fingerprint() -> str:
    parts = [platform.node(), platform.machine()]
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            parts.append(str(winreg.QueryValueEx(key, "MachineGuid")[0]))
    except (OSError, ImportError):
        parts.append(hex(uuid.getnode()))
    return hashlib.sha256("|".join(parts).lower().encode("utf-8")).hexdigest().upper()


def _activation_private_key() -> rsa.RSAPrivateKey:
    path = data_dir() / ACTIVATION_KEY_FILE
    if path.exists():
        pem = unprotect(path.read_text(encoding="utf-8")).encode("ascii")
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Activation key has an unexpected type")
        return key
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    path.write_text(protect(pem), encoding="utf-8")
    return key


def create_activation_request(
    path: Path, email: str, client_name: str, *, activation_mode: str = "offline"
) -> dict:
    key = _activation_private_key()
    identity = server_identity()
    request = {
        "format": 1,
        "product": PRODUCT,
        "product_version": __version__,
        "activation_mode": activation_mode,
        "request_id": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "operator_email": email.strip().lower(),
        "client_name": client_name.strip(),
        "server_name": identity.server_name,
        "mac_address": identity.mac_address,
        "machine_fingerprint": machine_fingerprint(),
        "encryption_public_key": base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii"),
    }
    path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    return request


def issue_license(
    request: dict,
    signing_key: Ed25519PrivateKey,
    smtp_password: str,
    expires_on: date,
    smtp_config: dict | None = None,
) -> dict:
    if request.get("product") != PRODUCT:
        raise ValueError("Activation request is for another product")
    public_key = serialization.load_der_public_key(
        base64.b64decode(request["encryption_public_key"])
    )
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise TypeError("Activation request encryption key is invalid")
    encrypted_password = public_key.encrypt(
        smtp_password.replace(" ", "").encode("utf-8"),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    payload = {
        "format": 1,
        "license_id": str(uuid.uuid4()),
        "product": PRODUCT,
        "issued_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "expires_on": expires_on.isoformat(),
        "request_id": request["request_id"],
        "operator_email": request["operator_email"],
        "client_name": request["client_name"],
        "server_name": request["server_name"],
        "machine_fingerprint": request["machine_fingerprint"],
        "smtp_password_encrypted": base64.b64encode(encrypted_password).decode("ascii"),
        "smtp_config": smtp_config
        or {
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "millensysnotifications@gmail.com",
            "sender_name": "MiMonitoringService",
            "use_tls": True,
        },
        "features": ["services", "iis", "disks", "performance", "reports"],
    }
    return {
        "payload": payload,
        "signature": base64.b64encode(signing_key.sign(_canonical(payload))).decode("ascii"),
    }


def verify_license(document: dict, *, today: date | None = None) -> LicenseStatus:
    try:
        payload = document["payload"]
        signature = base64.b64decode(document["signature"], validate=True)
        public_key = serialization.load_pem_public_key(LICENSE_PUBLIC_KEY_PEM.encode("ascii"))
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("Embedded license verification key is invalid")
        public_key.verify(signature, _canonical(payload))
        if payload.get("product") != PRODUCT:
            return LicenseStatus(False, "License is for another product.", payload)
        if payload.get("machine_fingerprint") != machine_fingerprint():
            return LicenseStatus(False, "License belongs to another server.", payload)
        expiry = date.fromisoformat(str(payload["expires_on"]))
        if (today or datetime.now(UTC).date()) > expiry:
            return LicenseStatus(False, f"License expired on {expiry.isoformat()}.", payload)
        return LicenseStatus(True, f"Licensed until {expiry.isoformat()}.", payload)
    except Exception as exc:  # noqa: BLE001
        return LicenseStatus(False, f"License validation failed: {exc}", {})


def current_license() -> LicenseStatus:
    path = data_dir() / LICENSE_FILE
    if not path.exists():
        return LicenseStatus(False, "MiMonitoringService is not activated.", {})
    try:
        return verify_license(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        return LicenseStatus(False, f"Cannot read the license: {exc}", {})


def import_license(source: Path) -> tuple[LicenseStatus, str]:
    document = json.loads(source.read_text(encoding="utf-8"))
    status = verify_license(document)
    if not status.valid:
        return status, ""
    encrypted = base64.b64decode(status.payload["smtp_password_encrypted"])
    password = _activation_private_key().decrypt(
        encrypted,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")
    destination = data_dir() / LICENSE_FILE
    if source.resolve() != destination.resolve():
        shutil.copyfile(source, destination)
    return status, password


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    return load_signing_key_data(path.read_bytes())


def load_signing_key_data(data: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Signing key has an unexpected type")
    return key
