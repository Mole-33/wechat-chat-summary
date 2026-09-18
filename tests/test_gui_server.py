import json
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from gui.server import GUIStateManager, run_gui_server, state


class TestGUIServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18991
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=run_gui_server, args=(cls.port,), daemon=True).start()
        time.sleep(0.5)

    def test_status_endpoint_has_privacy_safe_state(self):
        response = urllib.request.urlopen(f"{self.base_url}/api/status")
        self.assertEqual(response.status, 200)
        data = json.loads(response.read().decode("utf-8"))
        self.assertIn("wechat_running", data)
        self.assertIn("connected", data)
        self.assertNotIn("api_key", json.dumps(data).lower())

    def test_accounts_and_groups_endpoints(self):
        accounts = json.loads(urllib.request.urlopen(f"{self.base_url}/api/accounts").read())
        groups = json.loads(urllib.request.urlopen(f"{self.base_url}/api/groups").read())
        self.assertIn("accounts", accounts)
        self.assertIn("groups", groups)

    def test_static_dashboard(self):
        html = urllib.request.urlopen(f"{self.base_url}/").read().decode("utf-8")
        self.assertIn("微信群聊 AI 总结助手", html)
        self.assertIn("隐私边界", html)
        self.assertIn("立即开始总结", html)
        script = urllib.request.urlopen(f"{self.base_url}/app.js").read().decode("utf-8")
        self.assertIn("未保存 API Key", script)
        self.assertIn("未选择模型", script)
        self.assertNotIn("估算消息量与调用次数", html)
        self.assertEqual(urllib.request.urlopen(f"{self.base_url}/style.css").status, 200)
        self.assertEqual(urllib.request.urlopen(f"{self.base_url}/app.js").status, 200)

    def test_summary_starts_without_estimate_confirmation(self):
        payload = json.dumps({
            "group_ids": ["room@chatroom"],
            "start": "2026-09-17T00:00",
            "end": "2026-09-17T13:00",
            "provider_id": "provider-id",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/summary/run", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with patch.object(state, "start_summary_job", return_value="direct-job") as mocked:
            response = json.loads(urllib.request.urlopen(request).read())
        self.assertEqual(response["job_id"], "direct-job")
        mocked.assert_called_once()

    def test_missing_group_finishes_job_as_failed(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        manager.reader = SimpleNamespace(connected=True)
        manager.groups = {}
        manager.settings = SimpleNamespace(get=lambda _key, default=None: default)
        manager.summary_jobs = {}
        manager._client = lambda _provider_id, require_model=False: object()

        with patch("gui.server.threading.Thread") as thread_cls, patch("gui.server.notify"):
            job_id = manager.start_summary_job(
                ["missing@chatroom", "missing@chatroom"],
                datetime(2026, 9, 17, 0, 0),
                datetime(2026, 9, 17, 13, 0),
                "provider-id",
            )
            worker = thread_cls.call_args.kwargs["target"]
            worker()

        job = manager.summary_jobs[job_id]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(list(job["errors"]), ["missing@chatroom"])

    def test_summary_client_rejects_missing_key_before_reading_messages(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        manager.settings = SimpleNamespace(
            provider_with_secret=lambda _provider_id: {
                "id": "provider-id", "base_url": "https://api.example/v1",
                "model": "model-a", "api_key": "",
            }
        )
        with self.assertRaisesRegex(ValueError, "尚未保存 API Key"):
            manager._client("provider-id", require_model=True)

    def test_summary_client_rejects_missing_model_before_reading_messages(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        manager.settings = SimpleNamespace(
            provider_with_secret=lambda _provider_id: {
                "id": "provider-id", "base_url": "https://api.example/v1",
                "model": "", "api_key": "secret",
            }
        )
        with self.assertRaisesRegex(ValueError, "尚未选择模型"):
            manager._client("provider-id", require_model=True)


if __name__ == "__main__":
    unittest.main()
