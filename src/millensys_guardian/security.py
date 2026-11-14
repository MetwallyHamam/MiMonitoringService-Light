from __future__ import annotations

import base64
import sys

_ENTROPY = b"Millensys.ServerGuardian.v1"
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def protect(secret: str) -> str:
    if not secret:
        return ""
    if sys.platform != "win32":
        raise RuntimeError("Secret storage requires Windows DPAPI")
    import win32crypt

    encrypted = win32crypt.CryptProtectData(
        secret.encode("utf-8"),
        "MiMonitoringService",
        _ENTROPY,
        None,
        None,
        _CRYPTPROTECT_LOCAL_MACHINE,
    )
    return base64.b64encode(encrypted).decode("ascii")


def unprotect(token: str) -> str:
    if not token:
        return ""
    if sys.platform != "win32":
        raise RuntimeError("Secret storage requires Windows DPAPI")
    import win32crypt

    raw = base64.b64decode(token)
    return win32crypt.CryptUnprotectData(raw, _ENTROPY, None, None, 0)[1].decode("utf-8")
