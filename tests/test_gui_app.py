import os
import socket
import tempfile
import unittest
from pathlib import Path
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

    def test_browser_can_be_disabled_for_smoke_test(self):
        with patch.dict(os.environ, {"WECHAT_AI_SUMMARY_NO_BROWSER": "1"}):
            process, profile = gui_app.launch_app_window("http://127.0.0.1:18989")
        self.assertIsNone(process)
        self.assertIsNone(profile)

    def test_temporary_browser_profile_is_removed(self):
        with tempfile.TemporaryDirectory() as parent:
            profile = Path(parent) / "ui-test"
            profile.mkdir()
            (profile / "Preferences").write_text("test", encoding="utf-8")
            self.assertTrue(gui_app.remove_temporary_profile(profile, retries=1))
            self.assertFalse(profile.exists())


if __name__ == "__main__":
    unittest.main()
