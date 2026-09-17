import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.models import ChatMessage
from core.secure_settings import SettingsStore
from core.stats_engine import StatsEngine
from core.storage import StorageManager
from core.summarizer import chunk_messages, estimate_tokens, estimate_work, summary_to_markdown


class TestStatsAndPrivacy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "stats.db"
        self.storage = StorageManager(self.db_path)
        self.engine = StatsEngine()

    def tearDown(self):
        self.tmp.cleanup()

    def messages(self):
        return [
            ChatMessage(datetime(2026, 9, 17, 9, 0), "测试群", "张三", "确认今天发布"),
            ChatMessage(datetime(2026, 9, 17, 9, 3), "测试群", "李四", "我负责回归测试"),
            ChatMessage(datetime(2026, 9, 17, 10, 0), "测试群", "系统", "王五加入了群聊"),
        ]

    def test_aggregate_storage_never_creates_message_body_table(self):
        summary = self.engine.compute_daily_summary(self.messages(), "测试群", "2026-09-17")
        self.storage.replace_daily_summary("room@chatroom", summary)
        loaded = self.storage.get_daily_summary("room@chatroom", "2026-09-17")
        self.assertEqual(loaded.total_messages, 2)
        self.assertEqual(loaded.member_stats[0].message_count, 1)
        conn = sqlite3.connect(self.db_path)
        try:
            schema = " ".join(row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
            self.assertNotIn("content", schema.lower())
            self.assertNotIn("message_body", schema.lower())
        finally:
            conn.close()

    def test_token_estimate_and_chunks(self):
        messages = self.messages()[:2]
        self.assertGreater(estimate_tokens("中文 test"), 0)
        estimate = estimate_work(messages)
        self.assertEqual(estimate["message_count"], 2)
        self.assertEqual(len(chunk_messages(messages, limit=20)), 2)

    def test_markdown_contains_fixed_sections_and_evidence(self):
        data = {
            "group_name": "测试群", "start": "2026-09-17 00:00:00", "end": "2026-09-17 13:00:00", "message_count": 2,
            "core_summary": "确定发布安排。", "topics": [],
            "decisions": [{"text": "今天发布", "evidence": [{"sender": "张三", "time": "09:00", "quote": "确认今天发布"}]}],
            "action_items": [], "links_files": [], "open_questions": [], "active_members": [],
        }
        md = summary_to_markdown(data)
        self.assertIn("## 核心摘要", md)
        self.assertIn("## 待办事项", md)
        self.assertIn("张三", md)

    def test_api_key_is_dpapi_encrypted(self):
        settings = SettingsStore(Path(self.tmp.name) / "settings.json", Path(self.tmp.name) / "secrets.bin")
        profile = settings.upsert_provider({
            "kind": "openai", "name": "测试", "base_url": "https://example.invalid/v1",
            "model": "test-model", "api_key": "secret-api-key-value",
        })
        encrypted = (Path(self.tmp.name) / "secrets.bin").read_bytes()
        self.assertNotIn(b"secret-api-key-value", encrypted)
        restored = settings.provider_with_secret(profile["id"])
        self.assertEqual(restored["api_key"], "secret-api-key-value")


if __name__ == "__main__":
    unittest.main()
