import os
import socket
import unittest
from unittest.mock import patch

import gui_app


class TestLauncher(unittest.TestCase):
    def test_find_free_port_skips_occupied_port(self):
        listener = None
        for candidate in range(gui_app.PORT_START, gui_app.PORT_END):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", candidate))
                probe.listen()
                listener = probe
                break
            except OSError:
                probe.close()
        if listener is None:
            self.skipTest("测试端口范围已全部占用")
        with listener:
            self.assertGreater(gui_app.find_free_port(candidate), candidate)

    def test_default_browser_can_be_disabled_for_smoke_test(self):
        with patch.dict(os.environ, {"WECHAT_AI_SUMMARY_NO_BROWSER": "1"}):
            with patch("gui_app.webbrowser.open") as browser_open:
                opened = gui_app.launch_default_browser("http://127.0.0.1:18989")
        self.assertTrue(opened)
        browser_open.assert_not_called()

    def test_dashboard_uses_system_default_browser(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WECHAT_AI_SUMMARY_NO_BROWSER", None)
            with patch("gui_app.webbrowser.open", return_value=True) as browser_open:
                opened = gui_app.launch_default_browser("http://127.0.0.1:18989")
        self.assertTrue(opened)
        browser_open.assert_called_once_with("http://127.0.0.1:18989", new=2)

    def test_existing_instance_reopens_in_system_default_browser(self):
        with patch("gui_app.webbrowser.open", return_value=True) as browser_open:
            gui_app.open_existing_dashboard("http://127.0.0.1:18989")
        browser_open.assert_called_once_with("http://127.0.0.1:18989", new=2)


if __name__ == "__main__":
    unittest.main()
