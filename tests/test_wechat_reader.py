import sqlite3
import os
import tempfile
import unittest
from datetime import datetime

from listeners.wechat4_reader import EphemeralWeChatDB, WeChat4Reader
from vendor.wechat4_db import PAGE_SZ, WeChatDB, _decrypt_page


class RangeQueryTests(unittest.TestCase):
    def test_ephemeral_key_loading_rejects_a_different_running_account(self):
        db = EphemeralWeChatDB.__new__(EphemeralWeChatDB)
        db._db_files = []
        db.account = "wxid_selected_abcd"
        db.extract_master_key = lambda: ("00" * 32, 1, "wxid_running")
        db.extract_keys = lambda: self.fail("账号不匹配时不应继续扫描数据库密钥")

        with self.assertRaisesRegex(RuntimeError, "当前微信进程登录的是"):
            db._load_or_extract_keys()

        self.assertEqual(db._keys, {})
        self.assertIsNone(db.master_key)

    def test_ephemeral_key_cache_is_disabled(self):
        db = EphemeralWeChatDB.__new__(EphemeralWeChatDB)
        db._keys = {"message.db": b"secret"}
        self.assertIsNone(db._stable_key_file())
        self.assertIsNone(db._save_keys())

    def test_parallel_file_decryption_preserves_page_order(self):
        key = bytes(range(32))
        pages = [os.urandom(PAGE_SZ) for _ in range(5)]
        expected = b"".join(_decrypt_page(key, page, index) for index, page in enumerate(pages, 1))
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "encrypted.db")
            dst = os.path.join(tmp, "decrypted.db")
            with open(src, "wb") as handle:
                handle.write(b"".join(pages))
            WeChatDB.__new__(WeChatDB)._decrypt_file(src, dst, key)
            with open(dst, "rb") as handle:
                actual = handle.read()
        self.assertEqual(actual, expected)

    def test_database_range_query_filters_before_conversion(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE Msg_test(local_id INT, local_type INT, real_sender_id INT, "
            "create_time INT, message_content TEXT, source BLOB, packed_info_data BLOB, "
            "compress_content BLOB, server_id INT, sort_seq INT)"
        )
        conn.execute("CREATE INDEX Msg_test_SORTSEQ ON Msg_test(sort_seq)")
        conn.executemany(
            "INSERT INTO Msg_test VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (1, 1, 2, 100, "before", None, None, None, 1, 100_000),
                (2, 3, 2, 150, "image", None, None, None, 2, 150_000),
                (3, 1, 2, 180, "inside", None, None, None, 3, 180_000),
                (4, 1, 2, 220, "after", None, None, None, 4, 220_000),
                (5, (57 << 32) | 1, 2, 190, "composite text", None, None, None, 5, 190_000),
            ],
        )
        db = WeChatDB.__new__(WeChatDB)
        db._run_msg_query = lambda _user, build: build([(conn, "Msg_test")])
        db._sender_id_index = lambda: {}
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            rows = db.get_messages_in_range("room", 120, 200, text_only=True)
        finally:
            conn.close()
        self.assertEqual([row["content"] for row in rows], ["inside", "composite text"])
        self.assertTrue(any("INDEXED BY Msg_test_SORTSEQ" in sql for sql in statements))

    def test_database_range_query_merges_shards_in_time_order(self):
        conns = []
        for suffix, values in (
            ("a", [(1, 1, 2, 180, "later", None, None, None, 1, 180_000)]),
            ("b", [(1, 1, 3, 140, "earlier", None, None, None, 2, 140_000)]),
        ):
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            table = f"Msg_{suffix}"
            conn.execute(
                f"CREATE TABLE {table}(local_id INT, local_type INT, real_sender_id INT, "
                "create_time INT, message_content TEXT, source BLOB, packed_info_data BLOB, "
                "compress_content BLOB, server_id INT, sort_seq INT)"
            )
            conn.execute(f"CREATE INDEX {table}_SORTSEQ ON {table}(sort_seq)")
            conn.executemany(f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?,?)", values)
            conns.append((conn, table))
        db = WeChatDB.__new__(WeChatDB)
        db._run_msg_query = lambda _user, build: build(conns)
        db._sender_id_index = lambda: {}
        try:
            rows = db.get_messages_in_range("room", 120, 200, text_only=True)
        finally:
            for conn, _table in conns:
                conn.close()
        self.assertEqual([row["content"] for row in rows], ["earlier", "later"])

    def test_reader_uses_single_range_query_and_reports_progress(self):
        class FakeDB:
            wxid = "self"

            def get_messages_in_range(self, group_id, start_ts, end_ts, text_only, max_rows):
                self.args = (group_id, start_ts, end_ts, text_only, max_rows)
                return [{
                    "local_id": 1, "sort_seq": 11, "type": "文本", "sender_id": 2,
                    "sender_username": "", "create_time": start_ts, "content": "测试",
                }]

            def get_self_info(self):
                return {"nick_name": "我"}

        reader = WeChat4Reader()
        reader.db = FakeDB()
        events = []
        start = datetime(2026, 9, 17, 0, 0)
        end = datetime(2026, 9, 17, 13, 0)
        messages = reader.read_range(
            "room", "群", start, end, progress=lambda percent, text: events.append((percent, text)),
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(reader.db.args, ("room", int(start.timestamp()), int(end.timestamp()), True, 250_001))
        self.assertEqual(events[0][0], 5)
        self.assertEqual(events[-1][0], 100)

    def test_missing_numeric_sender_mapping_does_not_query_contact_per_message(self):
        class FakeDB:
            wxid = "self"

            def get_group_members(self, _group_id):
                return []

            def get_nickname(self, _sender_id):
                raise AssertionError("内部数字 sender_id 不应查询 contact.db")

        reader = WeChat4Reader()
        reader.db = FakeDB()
        message = reader._convert("room", "群", {
            "local_id": 1, "sort_seq": 1, "type": "文本", "sender_id": 999,
            "sender_username": "", "create_time": 1789574400, "content": "测试",
        })
        self.assertEqual(message.sender_nickname, "未知成员")


if __name__ == "__main__":
    unittest.main()
