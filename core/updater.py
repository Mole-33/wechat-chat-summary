"""Signed GitHub Release update checks and verified staging."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Dict

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from config import APP_VERSION, DATA_DIR, GITHUB_REPOSITORY


PUBLIC_KEY_B64 = "Osp4vuMtMiVsTxR4LSK2iM3j2b6xpB+rlP1yIEVHb10="
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"


def _canonical(payload: Dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _version_tuple(value: str):
    parts = []
    for item in value.lstrip("v").split("."):
        digits = "".join(ch for ch in item if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


class UpdateManager:
    def _get_json(self, url: str) -> Dict:
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "WeChat-AI-Summary"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def verify_manifest(self, manifest: Dict) -> Dict:
        signature = manifest.get("signature", "")
        unsigned = dict(manifest)
        unsigned.pop("signature", None)
        try:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
            key.verify(base64.b64decode(signature), _canonical(unsigned))
        except Exception:
            raise RuntimeError("更新清单数字签名无效，已拒绝下载") from None
        return unsigned

    def check(self) -> Dict:
        release = self._get_json(RELEASE_API)
        manifest_asset = next((a for a in release.get("assets", []) if a.get("name") == "update-manifest.json"), None)
        if not manifest_asset:
            return {"available": False, "message": "最新 Release 未提供签名更新清单"}
        manifest = self.verify_manifest(self._get_json(manifest_asset["browser_download_url"]))
        return {
            "available": _version_tuple(manifest.get("version", "0")) > _version_tuple(APP_VERSION),
            "current_version": APP_VERSION,
            "latest_version": manifest.get("version", ""),
            "adapter_version": manifest.get("adapter_version", ""),
            "asset": manifest.get("asset", {}),
            "message": "发现新版本" if _version_tuple(manifest.get("version", "0")) > _version_tuple(APP_VERSION) else "当前已是最新版本",
        }

    def stage(self, asset: Dict) -> Path:
        url, expected = asset.get("url", ""), asset.get("sha256", "")
        name = Path(asset.get("name") or "update.zip").name
        if not url.startswith("https://") or len(expected) != 64:
            raise ValueError("更新资源信息无效")
        target_dir = DATA_DIR / "updates"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        request = urllib.request.Request(url, headers={"User-Agent": "WeChat-AI-Summary"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=120) as response, open(target, "wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest().lower() != expected.lower():
            target.unlink(missing_ok=True)
            raise RuntimeError("更新包哈希校验失败，文件已删除")
        return target

