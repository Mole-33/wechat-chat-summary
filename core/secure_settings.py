"""Settings storage with Windows DPAPI protection for secrets."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import uuid
import urllib.parse
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import DEFAULT_CONFIG, PROVIDER_DEFAULTS, SECRETS_PATH, SETTINGS_PATH


def _looks_like_api_key(value: str) -> bool:
    value = str(value or "").strip()
    return value.startswith(("sk-", "sk_")) and "://" not in value


def _valid_api_url(value: str) -> bool:
    value = str(value or "").strip().lower()
    return value.startswith(("http://", "https://")) and "://" in value


def _normalize_provider_base_url(kind: str, value: str) -> str:
    """Repair known provider website URLs to their documented API base."""
    value = str(value or "").strip().rstrip("/")
    if kind != "siliconflow" or not _valid_api_url(value):
        return value
    try:
        hostname = (urllib.parse.urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return value
    if hostname == "siliconflow.cn" or hostname.endswith(".siliconflow.cn"):
        return PROVIDER_DEFAULTS["siliconflow"]["base_url"]
    return value


def _normalize_proxy_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("代理地址格式不正确，请填写例如 http://127.0.0.1:7890") from None
    return value.rstrip("/")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_bytes(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user with DPAPI."""
    if os.name != "nt":
        raise RuntimeError("敏感配置加密仅支持 Windows")
    in_blob, keepalive = _blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), "WeChatAISummary", None, None, None, 0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def unprotect_bytes(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("敏感配置解密仅支持 Windows")
    in_blob, keepalive = _blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class SettingsStore:
    """Thread-safe non-secret JSON plus one DPAPI-encrypted secret document."""

    def __init__(self, settings_path: Path = SETTINGS_PATH, secrets_path: Path = SECRETS_PATH):
        self.settings_path = Path(settings_path)
        self.secrets_path = Path(secrets_path)
        self._lock = threading.RLock()
        self._settings: Dict[str, Any] = {}
        self._secrets: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._settings = dict(DEFAULT_CONFIG)
            if self.settings_path.exists():
                try:
                    saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                    if isinstance(saved, dict):
                        self._settings.update(saved)
                except (OSError, json.JSONDecodeError):
                    pass
            self._settings.setdefault("providers", [])
            self._secrets = {}
            if self.secrets_path.exists():
                try:
                    raw = unprotect_bytes(self.secrets_path.read_bytes())
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict):
                        self._secrets = parsed
                except Exception:
                    self._secrets = {}
            if self._repair_provider_settings():
                self.save()

    def _repair_provider_settings(self) -> bool:
        """Remove accidentally plaintext API keys from URL fields and restore provider defaults."""
        changed = False
        providers = list(self._settings.get("providers", []))
        api_keys = self._secrets.setdefault("api_keys", {})
        for profile in providers:
            kind = str(profile.get("kind") or "custom").lower()
            base_url = str(profile.get("base_url") or "").strip()
            if _looks_like_api_key(base_url):
                profile_id = str(profile.get("id") or "")
                if profile_id and not api_keys.get(profile_id):
                    api_keys[profile_id] = base_url
                profile["base_url"] = PROVIDER_DEFAULTS.get(kind, {}).get("base_url", "")
                changed = True
            elif kind in PROVIDER_DEFAULTS and kind != "custom":
                # Built-in providers always use the bundled official API base.
                repaired = PROVIDER_DEFAULTS[kind]["base_url"]
                if repaired != base_url.rstrip("/"):
                    profile["base_url"] = repaired
                    changed = True
            else:
                normalized = base_url.rstrip("/")
                if normalized != base_url:
                    profile["base_url"] = normalized
                    changed = True
        if changed:
            self._settings["providers"] = providers
        return changed

    def _write_json_atomic(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def save(self) -> None:
        with self._lock:
            self._write_json_atomic(self.settings_path, self._settings)
            self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
            encrypted = protect_bytes(json.dumps(self._secrets, ensure_ascii=False).encode("utf-8"))
            tmp = self.secrets_path.with_suffix(".tmp")
            tmp.write_bytes(encrypted)
            os.replace(tmp, self.secrets_path)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._settings[key] = value
            self.save()

    def public_settings(self) -> Dict[str, Any]:
        with self._lock:
            return {
                **{k: v for k, v in self._settings.items() if k != "providers"},
                "providers": self.list_providers(),
                "proxy_password_saved": bool(self._secrets.get("proxy_password")),
            }

    def list_providers(self) -> List[Dict[str, Any]]:
        with self._lock:
            api_keys = self._secrets.get("api_keys", {})
            result = []
            for profile in self._settings.get("providers", []):
                item = dict(profile)
                item["api_key_saved"] = bool(api_keys.get(item.get("id", "")))
                result.append(item)
            return result

    def provider_with_secret(self, profile_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for profile in self._settings.get("providers", []):
                if profile.get("id") == profile_id:
                    item = dict(profile)
                    item["api_key"] = self._secrets.get("api_keys", {}).get(profile_id, "")
                    item["proxy_password"] = self._secrets.get("proxy_password", "")
                    return item
        return None

    def upsert_provider(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            kind = str(payload.get("kind", "custom")).strip().lower()
            if kind not in PROVIDER_DEFAULTS:
                kind = "custom"
            profile_id = str(payload.get("id") or uuid.uuid4().hex)
            default = PROVIDER_DEFAULTS[kind]
            raw_submitted_base_url = str(payload.get("base_url") or "").strip()
            submitted_api_key = payload.get("api_key")
            if _looks_like_api_key(raw_submitted_base_url) and not submitted_api_key:
                # Migrate values saved by older versions where the key could be
                # pasted into the address field, even though built-ins now hide it.
                submitted_api_key = raw_submitted_base_url
            submitted_base_url = (
                raw_submitted_base_url
                if kind == "custom"
                else default["base_url"]
            )
            if kind == "custom" and _looks_like_api_key(submitted_base_url):
                if not submitted_api_key:
                    submitted_api_key = submitted_base_url
                submitted_base_url = default["base_url"]
            if submitted_base_url and not _valid_api_url(submitted_base_url):
                raise ValueError("API 地址必须以 http:// 或 https:// 开头，不能填写 API Key")
            submitted_base_url = _normalize_provider_base_url(kind, submitted_base_url)
            profile = {
                "id": profile_id,
                "kind": kind,
                "name": str(payload.get("name") or default["name"]).strip(),
                "base_url": str(submitted_base_url or default["base_url"]).strip().rstrip("/"),
                "model": str(payload.get("model") or "").strip(),
                "timeout_seconds": max(10, min(int(payload.get("timeout_seconds", 180)), 600)),
            }
            providers = list(self._settings.get("providers", []))
            for index, existing in enumerate(providers):
                if existing.get("id") == profile_id:
                    providers[index] = profile
                    break
            else:
                providers.append(profile)
            self._settings["providers"] = providers
            api_key = submitted_api_key
            if api_key is not None and str(api_key).strip():
                self._secrets.setdefault("api_keys", {})[profile_id] = str(api_key).strip()
            self.save()
            return {**profile, "api_key_saved": bool(self._secrets.get("api_keys", {}).get(profile_id))}

    def delete_provider(self, profile_id: str) -> None:
        with self._lock:
            self._settings["providers"] = [
                item for item in self._settings.get("providers", []) if item.get("id") != profile_id
            ]
            self._secrets.get("api_keys", {}).pop(profile_id, None)
            if self._settings.get("active_provider_id") == profile_id:
                self._settings["active_provider_id"] = ""
            self.save()

    def set_proxy(self, url: str, username: str = "", password: Optional[str] = None) -> None:
        with self._lock:
            self._settings["proxy_url"] = _normalize_proxy_url(url)
            self._settings["proxy_username"] = str(username or "").strip()
            if password is not None:
                if password:
                    self._secrets["proxy_password"] = str(password)
                else:
                    self._secrets.pop("proxy_password", None)
            self.save()
