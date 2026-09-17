"""Build and Ed25519-sign an update manifest for a GitHub Release asset."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--adapter-version", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--output", default="update-manifest.json")
    args = parser.parse_args()
    asset = Path(args.asset)
    payload = {
        "version": args.version,
        "adapter_version": args.adapter_version,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "asset": {"url": args.url, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(), "name": asset.name},
    }
    key = serialization.load_pem_private_key(Path(args.private_key).read_bytes(), password=None)
    payload["signature"] = base64.b64encode(key.sign(canonical(payload))).decode("ascii")
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

