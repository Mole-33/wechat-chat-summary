"""Generate the release-signing keypair. Keep the private key out of Git."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / ".secrets"
PRIVATE_PATH = SECRET_DIR / "update-signing-key.pem"
PUBLIC_PATH = SECRET_DIR / "update-public-key.txt"


def main():
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVATE_PATH.exists():
        raise SystemExit(f"密钥已存在，未覆盖：{PRIVATE_PATH}")
    key = Ed25519PrivateKey.generate()
    PRIVATE_PATH.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    encoded = base64.b64encode(public).decode("ascii")
    PUBLIC_PATH.write_text(encoded, encoding="ascii")
    print(encoded)


if __name__ == "__main__":
    main()

