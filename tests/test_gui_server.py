import json
import threading
import time
import unittest
import urllib.request

from gui.server import run_gui_server


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
        self.assertEqual(urllib.request.urlopen(f"{self.base_url}/style.css").status, 200)
        self.assertEqual(urllib.request.urlopen(f"{self.base_url}/app.js").status, 200)


if __name__ == "__main__":
    unittest.main()

