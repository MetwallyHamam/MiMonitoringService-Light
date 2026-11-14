from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

project = Path(__file__).resolve().parents[1]
private_dir = project / "support-private"
private_dir.mkdir(exist_ok=True)
private_path = private_dir / "license-signing-private.pem"
public_module = project / "src" / "millensys_guardian" / "license_public_key.py"

if private_path.exists():
    private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
else:
    private_key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

public_pem = private_key.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("ascii")
public_module.write_text(
    '"""Generated Millensys license verification key. Contains no private material."""\n\n'
    f"LICENSE_PUBLIC_KEY_PEM = {public_pem!r}\n",
    encoding="utf-8",
)
print(f"Private signing key: {private_path}")
print(f"Public verification module: {public_module}")
