import io
import json
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from config import PROVIDER_DEFAULTS
from core.ai_client import APIClientError, CompatibleAIClient, _endpoint
from core.secure_settings import SettingsStore


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TestCompatibleAIClient(unittest.TestCase):
    def test_endpoint_accepts_base_or_full_endpoint(self):
        self.assertEqual(_endpoint("https://api.siliconflow.cn/v1", "models"), "https://api.siliconflow.cn/v1/models")
        self.assertEqual(_endpoint("https://api.example/v1/chat/completions", "models"), "https://api.example/v1/models")
        self.assertEqual(_endpoint("https://api.example/v1/models", "chat/completions"), "https://api.example/v1/chat/completions")

    def test_endpoint_rejects_api_key(self):
        with self.assertRaisesRegex(APIClientError, "不能填写 API Key"):
            _endpoint("sk-secret", "models")

    def test_siliconflow_model_payload(self):
        client = CompatibleAIClient({"base_url": "https://api.siliconflow.cn/v1", "api_key": "test"})
        with patch.object(client.opener, "open", return_value=_Response({"data": [{"id": "Qwen/Test"}]})):
            self.assertEqual(client.list_models(), ["Qwen/Test"])

    def test_siliconflow_web_console_url_is_replaced_with_official_api(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = SettingsStore(root / "settings.json", root / "secrets.bin")
            profile = store.upsert_provider({
                "kind": "siliconflow",
                "base_url": "https://cloud.siliconflow.cn/chat/completions",
                "api_key": "secret",
            })
            self.assertEqual(profile["base_url"], PROVIDER_DEFAULTS["siliconflow"]["base_url"])

    def test_builtin_provider_ignores_submitted_api_address(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = SettingsStore(root / "settings.json", root / "secrets.bin")
            profile = store.upsert_provider({
                "kind": "openai", "base_url": "http://127.0.0.1:9", "api_key": "secret",
            })
            self.assertEqual(profile["base_url"], PROVIDER_DEFAULTS["openai"]["base_url"])

    def test_proxy_address_adds_http_scheme(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = SettingsStore(root / "settings.json", root / "secrets.bin")
            store.set_proxy("127.0.0.1:7890")
            self.assertEqual(store.get("proxy_url"), "http://127.0.0.1:7890")

    def test_siliconflow_login_redirect_has_actionable_error(self):
        client = CompatibleAIClient({
            "kind": "siliconflow", "base_url": "https://cloud.siliconflow.cn",
            "api_key": "test", "model": "test-model",
        })
        headers = Message()
        headers["Location"] = "https://accounts.siliconflow.cn/login"
        error = urllib.error.HTTPError(
            "https://cloud.siliconflow.cn/chat/completions", 307, "redirect", headers, io.BytesIO(b""),
        )
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaisesRegex(APIClientError, "网页登录页"):
                client.chat("system", "user")

    def test_refused_proxy_has_actionable_error(self):
        client = CompatibleAIClient(
            {"base_url": "https://api.example/v1", "api_key": "test"},
            proxy_url="http://127.0.0.1:7890",
        )
        error = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaisesRegex(APIClientError, "无法连接代理 127.0.0.1:7890"):
                client.list_models()

    def test_misplaced_key_is_encrypted_and_url_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = SettingsStore(root / "settings.json", root / "secrets.bin")
            profile = store.upsert_provider({"kind": "siliconflow", "base_url": "sk-accidentally-pasted-here"})
            restored = store.provider_with_secret(profile["id"])
            self.assertEqual(restored["base_url"], PROVIDER_DEFAULTS["siliconflow"]["base_url"])
            self.assertEqual(restored["api_key"], "sk-accidentally-pasted-here")
            self.assertNotIn("sk-accidentally-pasted-here", (root / "settings.json").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
