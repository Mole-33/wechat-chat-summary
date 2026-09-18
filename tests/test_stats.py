import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.models import ChatMessage
from core.secure_settings import SettingsStore
from core.stats_engine import StatsEngine
from core.storage import StorageManager
from core.summarizer import chunk_messages, estimate_tokens, summarize_messages, summary_to_markdown


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

    def test_token_counting_and_chunks(self):
        messages = self.messages()[:2]
        self.assertGreater(estimate_tokens("中文 test"), 0)
        self.assertEqual(len(chunk_messages(messages, limit=20)), 2)

    def test_same_or_unknown_names_do_not_merge_different_senders(self):
        messages = [
            ChatMessage(
                datetime(2026, 9, 17, 9, 0), "测试群", "同名", "第一条",
                sender_id="wxid_one",
            ),
            ChatMessage(
                datetime(2026, 9, 17, 9, 1), "测试群", "同名", "第二条",
                sender_id="wxid_two",
            ),
            ChatMessage(
                datetime(2026, 9, 17, 9, 2), "测试群", "未知成员", "第三条",
                sender_id="101",
            ),
            ChatMessage(
                datetime(2026, 9, 17, 9, 3), "测试群", "未知成员", "第四条",
                sender_id="102",
            ),
            ChatMessage(
                datetime(2026, 9, 17, 9, 4), "测试群", "同名", "第五条",
                sender_id="nickname:同名",
            ),
            ChatMessage(
                datetime(2026, 9, 17, 9, 5), "测试群", "同名", "第六条",
            ),
        ]
        summary = self.engine.compute_daily_summary(messages, "测试群", "2026-09-17")
        self.assertEqual(summary.total_members_spoke, 6)
        self.assertEqual([item.message_count for item in summary.member_stats], [1, 1, 1, 1, 1, 1])
        self.storage.replace_daily_summary("room@chatroom", summary)
        loaded = self.storage.get_daily_summary("room@chatroom", "2026-09-17")
        self.assertEqual(loaded.total_members_spoke, 6)
        self.assertEqual(len(loaded.member_stats), 6)
        self.assertEqual(len({item.member_key for item in loaded.member_stats}), 6)
        self.assertFalse(any("wxid" in item.member_key for item in loaded.member_stats))

    def test_legacy_nickname_keyed_member_stats_are_migrated(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            """CREATE TABLE daily_member_stats (
                group_id TEXT NOT NULL, date_str TEXT NOT NULL, nickname TEXT NOT NULL,
                message_count INTEGER NOT NULL, total_words INTEGER NOT NULL,
                first_time TEXT, last_time TEXT, rank_no INTEGER NOT NULL,
                PRIMARY KEY (group_id, date_str, nickname)
            )"""
        )
        conn.execute(
            "INSERT INTO daily_member_stats VALUES(?,?,?,?,?,?,?,?)",
            ("room", "2026-09-16", "历史昵称", 3, 12, "09:00", "10:00", 1),
        )
        conn.commit()
        conn.close()

        StorageManager(legacy_path)
        conn = sqlite3.connect(legacy_path)
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(daily_member_stats)")]
            row = conn.execute("SELECT member_key,nickname FROM daily_member_stats").fetchone()
        finally:
            conn.close()
        self.assertIn("member_key", columns)
        self.assertEqual(row, ("legacy:历史昵称", "历史昵称"))

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

    def test_summary_reports_ai_call_progress(self):
        class Client:
            def chat(self, _system, _user):
                return {"content": '{"core_summary":"完成"}', "usage": {}}

        events = []
        messages = self.messages()[:1]
        summarize_messages(
            Client(), "测试群", messages[0].timestamp, messages[0].timestamp,
            messages, lambda current, total, text: events.append((current, total, text)),
        )
        self.assertEqual(events[0][0], 0)
        self.assertEqual(events[-1][0], events[-1][1])
        self.assertIn("正在调用 AI", events[0][2])

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
