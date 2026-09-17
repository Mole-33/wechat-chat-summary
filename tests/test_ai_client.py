import json
import tempfile
import unittest
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
