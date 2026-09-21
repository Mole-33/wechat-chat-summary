import json
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import gui_app
from gui.server import GUIStateManager, run_gui_server, state


class TestGUIServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 18991
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=run_gui_server, args=(cls.port, False), daemon=True).start()
        time.sleep(0.5)

    def test_status_endpoint_has_privacy_safe_state(self):
        response = urllib.request.urlopen(f"{self.base_url}/api/status")
        self.assertEqual(response.status, 200)
        data = json.loads(response.read().decode("utf-8"))
        self.assertIn("wechat_running", data)
        self.assertIn("connected", data)
        self.assertIn("startup_state", data)
        self.assertIn("active_summary_job_id", data)
        self.assertIn("latest_summary_job_id", data)
        self.assertIn("monitor_error", data)
        self.assertNotIn("api_key", json.dumps(data).lower())

    def test_windowed_launcher_provides_writable_standard_streams(self):
        original_stdout, original_stderr = gui_app.sys.stdout, gui_app.sys.stderr
        created_before = len(gui_app._NULL_STREAMS)
        try:
            gui_app.sys.stdout = None
            gui_app.sys.stderr = None
            gui_app.ensure_standard_streams()
            gui_app.sys.stdout.write("")
            gui_app.sys.stderr.write("")
            self.assertEqual(len(gui_app._NULL_STREAMS), created_before + 2)
        finally:
            gui_app.sys.stdout = original_stdout
            gui_app.sys.stderr = original_stderr
            for stream in gui_app._NULL_STREAMS[created_before:]:
                stream.close()
            del gui_app._NULL_STREAMS[created_before:]

    def test_saved_account_and_groups_auto_resume_monitoring(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        values = {
            "selected_account": "account-a",
            "selected_groups": [{"id": "room@chatroom", "name": "旧群名"}],
        }
        manager.settings = SimpleNamespace(
            get=lambda key, default=None: values.get(key, default),
            set=lambda key, value: values.__setitem__(key, value),
        )
        manager.groups = {}
        manager.should_exit = False
        manager.startup_state = "idle"
        manager.startup_message = ""
        manager._startup_restore_started = False
        manager._startup_restore_thread = None
        manager._lock = threading.RLock()

        def connect(account):
            self.assertEqual(account, "account-a")
            manager.groups = {
                "room@chatroom": {"id": "room@chatroom", "name": "新群名"},
            }

        manager.connect = connect
        manager.start_monitoring = lambda: {"success": True, "message": "已自动开启"}

        with patch("gui.server.threading.Thread") as thread_cls:
            self.assertTrue(manager.restore_saved_session_async())
            thread_cls.call_args.kwargs["target"](*thread_cls.call_args.kwargs["args"])

        self.assertEqual(manager.startup_state, "monitoring")
        self.assertEqual(manager.startup_message, "已自动开启")
        self.assertEqual(values["selected_groups"][0]["name"], "新群名")
        self.assertFalse(manager.restore_saved_session_async())

    def test_auto_resume_skips_when_no_saved_groups(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        manager.settings = SimpleNamespace(
            get=lambda key, default=None: "account-a" if key == "selected_account" else [],
        )
        manager.startup_state = "unused"
        manager.startup_message = "unused"
        self.assertFalse(manager.restore_saved_session_async())
        self.assertEqual(manager.startup_state, "idle")

    def test_accounts_and_groups_endpoints(self):
        accounts = json.loads(urllib.request.urlopen(f"{self.base_url}/api/accounts").read())
        groups = json.loads(urllib.request.urlopen(f"{self.base_url}/api/groups").read())
        self.assertIn("accounts", accounts)
        self.assertIn("groups", groups)

    def test_connect_rolls_back_partial_connection_when_group_read_fails(self):
        manager = GUIStateManager.__new__(GUIStateManager)

        class FakeReader:
            def __init__(self):
                self.closed = False

            def connect(self, _account):
                return {"account": "account-a", "wxid": "wxid_a", "nickname": "A"}

            def groups(self):
                raise RuntimeError("不兼容的群聊表")

            def close(self):
                self.closed = True

        manager.reader = FakeReader()
        manager._db_lock = threading.RLock()
        manager.account_info = {"account": "old"}
        manager.groups = {"old": {}}
        manager.stop_monitoring = lambda: None
        with self.assertRaisesRegex(RuntimeError, "不兼容的群聊表"):
            manager.connect("account-a")
        self.assertTrue(manager.reader.closed)
        self.assertEqual(manager.account_info, {})
        self.assertEqual(manager.groups, {})

    def test_group_change_while_monitoring_starts_new_group_at_current_tail(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        values = {}
        manager.settings = SimpleNamespace(
            set=lambda key, value: values.__setitem__(key, value),
        )
        manager.groups = {
            "old@chatroom": {"id": "old@chatroom", "name": "旧群"},
            "new@chatroom": {"id": "new@chatroom", "name": "新群"},
        }
        manager.reader = SimpleNamespace(latest_sequence=lambda group_id: 88 if group_id == "new@chatroom" else 0)
        manager.is_monitoring = True
        manager.monitor_watermarks = {"old@chatroom": 42, "removed@chatroom": 7}
        manager._lock = threading.RLock()
        manager._db_lock = threading.RLock()

        manager.select_groups([
            {"id": "old@chatroom", "name": "前端旧名称"},
            {"id": "new@chatroom", "name": "前端新名称"},
        ])

        self.assertEqual(manager.monitor_watermarks, {
            "old@chatroom": 42, "new@chatroom": 88,
        })
        self.assertEqual(values["selected_groups"], [
            {"id": "old@chatroom", "name": "旧群"},
            {"id": "new@chatroom", "name": "新群"},
        ])

    def test_stale_monitor_thread_cannot_stop_replacement_thread(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        manager._lock = threading.RLock()
        manager._stop_monitor = threading.Event()
        manager._monitor_thread = object()
        manager.is_monitoring = True
        stopped = threading.Event()
        stopped.set()

        manager._monitor_loop(stopped)

        self.assertTrue(manager.is_monitoring)
        self.assertIsNotNone(manager._monitor_thread)

    def test_live_messages_from_multiple_groups_are_globally_time_sorted(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        selected = [
            {"id": "newer@chatroom", "name": "新消息群"},
            {"id": "older@chatroom", "name": "旧消息群"},
        ]
        manager.settings = SimpleNamespace(get=lambda _key, default=None: selected)
        manager._lock = threading.RLock()
        manager._db_lock = threading.RLock()
        manager.monitor_watermarks = {item["id"]: 0 for item in selected}
        manager.monitor_failures = {}
        manager.monitor_error = ""
        manager.live_messages = []
        manager.account_info = {}
        manager.is_monitoring = True

        class Reader:
            def read_new(self, group_id, _name, _since):
                timestamp = datetime(
                    2026, 9, 21, 12 if group_id.startswith("newer") else 11, 0,
                )
                message = SimpleNamespace(
                    timestamp=timestamp, sender_nickname="成员", content=group_id,
                    sender_id="wxid_sender",
                )
                return {"messages": [message], "latest_seq": 1}

        manager.reader = Reader()
        class OneCycleEvent:
            def __init__(self):
                self.waits = 0

            def wait(self, _timeout):
                self.waits += 1
                return self.waits > 1

            def is_set(self):
                return False

        stop_event = OneCycleEvent()
        manager._monitor_thread = threading.current_thread()
        with patch.dict("gui.server.DEFAULT_CONFIG", {"poll_interval_seconds": 0}):
            manager._monitor_loop(stop_event)

        self.assertEqual(
            [item["group"] for item in manager.live_messages],
            ["新消息群", "旧消息群"],
        )

    def test_static_dashboard(self):
        html = urllib.request.urlopen(f"{self.base_url}/").read().decode("utf-8")
        self.assertIn("微信群聊 AI 总结助手", html)
        self.assertIn("隐私边界", html)
        self.assertIn("立即开始总结", html)
        script = urllib.request.urlopen(f"{self.base_url}/app.js").read().decode("utf-8")
        self.assertIn("未保存 API Key", script)
        self.assertIn("未选择模型", script)
        self.assertIn('id="providerBaseUrlFields" class="hidden"', html)
        self.assertIn("内置平台固定使用官方 API", html)
        self.assertIn('id="groupToggle"', html)
        self.assertIn('id="groupDropdown" class="group-dropdown hidden"', html)
        self.assertIn("toggleGroupDropdown", script)
        self.assertIn('minimax: ["MiniMax", "https://api.minimax.cn/v1"]', script)
        self.assertIn("resumeActiveJob", script)
        self.assertIn("latest_summary_job_id", script)
        self.assertIn("进度连接短暂中断", script)
        self.assertIn("读取异常", script)
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
        self.assertEqual(manager.active_summary_job_id(), "")

    def test_rejects_second_summary_while_one_is_active(self):
        manager = GUIStateManager.__new__(GUIStateManager)
        manager.reader = SimpleNamespace(connected=True)
        manager.summary_jobs = {
            "running-job": {"id": "running-job", "status": "running"},
        }
        manager._current_summary_job_id = "running-job"
        manager._summary_job_lock = threading.RLock()
        manager._client = lambda *_args, **_kwargs: self.fail("不应在重复任务时创建客户端")
        with self.assertRaisesRegex(ValueError, "已有总结任务正在运行"):
            manager.start_summary_job(
                ["room@chatroom"],
                datetime(2026, 9, 17, 0, 0),
                datetime(2026, 9, 17, 13, 0),
                "provider-id",
            )

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

    def test_siliconflow_preflight_failure_does_not_read_wechat_messages(self):
        class Reader:
            connected = True

            def read_range(self, *_args, **_kwargs):
                raise AssertionError("连接检查失败时不应读取微信消息")

        class Client:
            profile = {"kind": "siliconflow"}

            def list_models(self):
                raise RuntimeError("平台连接失败")

        manager = GUIStateManager.__new__(GUIStateManager)
        manager.reader = Reader()
        manager.groups = {"room@chatroom": {"id": "room@chatroom", "name": "测试群"}}
        manager.settings = SimpleNamespace(get=lambda _key, default=None: default)
        manager.summary_jobs = {}
        manager._client = lambda _provider_id, require_model=False: Client()

        with patch("gui.server.threading.Thread") as thread_cls, patch("gui.server.notify"):
            job_id = manager.start_summary_job(
                ["room@chatroom"],
                datetime(2026, 9, 17, 0, 0),
                datetime(2026, 9, 17, 13, 0),
                "provider-id",
            )
            thread_cls.call_args.kwargs["target"]()

        job = manager.summary_jobs[job_id]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["progress"], 100)
        self.assertIn("尚未读取微信消息", job["message"])

    def test_minimax_preflight_failure_does_not_read_wechat_messages(self):
        class Reader:
            connected = True

            def read_range(self, *_args, **_kwargs):
                raise AssertionError("连接检查失败时不应读取微信消息")

        class Client:
            profile = {"kind": "minimax"}

            def list_models(self):
                raise RuntimeError("API key 无效")

        manager = GUIStateManager.__new__(GUIStateManager)
        manager.reader = Reader()
        manager.groups = {"room@chatroom": {"id": "room@chatroom", "name": "测试群"}}
        manager.settings = SimpleNamespace(get=lambda _key, default=None: default)
        manager.summary_jobs = {}
        manager._client = lambda _provider_id, require_model=False: Client()

        with patch("gui.server.threading.Thread") as thread_cls, patch("gui.server.notify"):
            job_id = manager.start_summary_job(
                ["room@chatroom"],
                datetime(2026, 9, 17, 0, 0),
                datetime(2026, 9, 17, 13, 0),
                "provider-id",
            )
            thread_cls.call_args.kwargs["target"]()

        job = manager.summary_jobs[job_id]
        self.assertEqual(job["status"], "failed")
        self.assertIn("尚未读取微信消息", job["message"])
        self.assertIn("API key 无效", job["errors"]["room@chatroom"])


if __name__ == "__main__":
    unittest.main()
