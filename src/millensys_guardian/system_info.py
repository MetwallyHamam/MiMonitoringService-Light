from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerIdentity:
    server_name: str
    mac_address: str


def server_identity() -> ServerIdentity:
    node = uuid.getnode()
    mac = ":".join(f"{(node >> offset) & 0xFF:02X}" for offset in range(40, -1, -8))
    return ServerIdentity(socket.gethostname() or "Unknown server", mac)

