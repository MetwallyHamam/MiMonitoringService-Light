import base64
from datetime import date, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from millensys_guardian import licensing


def request_document(fingerprint: str = "SERVER-FINGERPRINT") -> tuple[dict, rsa.RSAPrivateKey]:
    encryption_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    request = {
        "product": licensing.PRODUCT,
        "request_id": "request-1",
        "operator_email": "engineer@millensys.com",
        "client_name": "Test Client",
        "server_name": "SERVER01",
        "machine_fingerprint": fingerprint,
        "encryption_public_key": base64.b64encode(
            encryption_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode("ascii"),
    }
    return request, encryption_key


def test_signed_license_has_expiration_and_machine_binding(monkeypatch):
    signing_key = Ed25519PrivateKey.generate()
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    monkeypatch.setattr(licensing, "LICENSE_PUBLIC_KEY_PEM", public_pem)
    monkeypatch.setattr(licensing, "machine_fingerprint", lambda: "SERVER-FINGERPRINT")
    request, _ = request_document()
    expiry = date.today() + timedelta(days=30)

    document = licensing.issue_license(request, signing_key, "secret", expiry)

    status = licensing.verify_license(document, today=date.today())
    assert status.valid
    assert status.payload["expires_on"] == expiry.isoformat()
    assert not licensing.verify_license(document, today=expiry + timedelta(days=1)).valid
    monkeypatch.setattr(licensing, "machine_fingerprint", lambda: "OTHER-SERVER")
    assert not licensing.verify_license(document, today=date.today()).valid


def test_modified_license_signature_is_rejected(monkeypatch):
    signing_key = Ed25519PrivateKey.generate()
    public_pem = signing_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    monkeypatch.setattr(licensing, "LICENSE_PUBLIC_KEY_PEM", public_pem)
    monkeypatch.setattr(licensing, "machine_fingerprint", lambda: "SERVER-FINGERPRINT")
    request, _ = request_document()
    document = licensing.issue_license(
        request, signing_key, "secret", date.today() + timedelta(days=30)
    )
    document["payload"]["expires_on"] = "2099-12-31"

    assert not licensing.verify_license(document).valid
