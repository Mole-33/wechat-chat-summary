import sqlite3
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from listeners.wechat4_reader import EphemeralWeChatDB, WeChat4Reader
from vendor.wechat4_db import (
    PAGE_SZ,
    WeChatDB,
    _chatroom_display_name_index,
    _decrypt_page,
)


def _varint(value):
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _bytes_field(number, value):
    value = value if isinstance(value, bytes) else value.encode("utf-8")
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _chatroom_member_record(username, display_name=""):
    member = _bytes_field(1, username)
    if display_name:
        member += _bytes_field(2, display_name)
    return _bytes_field(1, member)


class RangeQueryTests(unittest.TestCase):
    def test_chatroom_display_names_are_decoded_from_ext_buffer(self):
        blob = (
            _chatroom_member_record("wxid_alice", "Alice 的群昵称")
            + _chatroom_member_record("wxid_bob")
            + _bytes_field(9, b"ignored")
        )
        self.assertEqual(
            _chatroom_display_name_index(blob),
            {"wxid_alice": "Alice 的群昵称"},
        )
        self.assertEqual(_chatroom_display_name_index(b"\x0a\xff"), {})

    def test_group_members_include_group_specific_display_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "contact.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE chat_room(id INTEGER, username TEXT, owner TEXT, ext_buffer BLOB);"
                "CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);"
                "CREATE TABLE contact(id INTEGER, username TEXT, nick_name TEXT, remark TEXT);"
            )
            ext_buffer = _chatroom_member_record("wxid_alice", "群内 Alice")
            conn.execute("INSERT INTO chat_room VALUES(1, 'room@chatroom', 'wxid_alice', ?)", (ext_buffer,))
            conn.execute("INSERT INTO chatroom_member VALUES(1, 7)")
            conn.execute("INSERT INTO contact VALUES(7, 'wxid_alice', '微信 Alice', '备注 Alice')")
            conn.commit()
            conn.close()

            db = WeChatDB.__new__(WeChatDB)

            def contact_conn():
                current = sqlite3.connect(path)
                current.row_factory = sqlite3.Row
                return current

            db._contact_conn = contact_conn
            members = db.get_group_members("room@chatroom")

        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["display_name"], "群内 Alice")
        self.assertTrue(members[0]["is_owner"])

    def test_reader_prefers_group_name_for_members_and_self(self):
        class FakeDB:
            wxid = "wxid_self"

            def __init__(self):
                self.member_reads = 0

            def get_group_members(self, _group_id):
                self.member_reads += 1
                return [
                    {
                        "username": "wxid_alice", "display_name": "群内 Alice",
                        "remark": "备注 Alice", "nick_name": "微信 Alice",
                    },
                    {
                        "username": "wxid_self", "display_name": "群内的我",
                        "remark": "", "nick_name": "账号昵称",
                    },
                ]

            def get_self_info(self):
                return {"nick_name": "账号昵称"}

        reader = WeChat4Reader()
        reader.db = FakeDB()
        incoming = reader._convert("room", "群", {
            "local_id": 1, "sort_seq": 1, "type": "文本", "sender_id": 7,
            "sender_username": "wxid_alice", "create_time": 1789574400, "content": "测试",
        })
        outgoing = reader._convert("room", "群", {
            "local_id": 2, "sort_seq": 2, "type": "文本", "sender_id": 2,
            "sender_username": "", "create_time": 1789574401, "content": "测试",
        })
        self.assertEqual(incoming.sender_nickname, "群内 Alice")
        self.assertEqual(outgoing.sender_nickname, "群内的我")
        self.assertEqual(reader.db.member_reads, 1)

        with patch("listeners.wechat4_reader.time.monotonic", return_value=10_000):
            reader._member_cache_time["room"] = 0
            reader._members("room")
        self.assertEqual(reader.db.member_reads, 2)

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

            def get_group_members(self, _group_id):
                return []

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
                return [{
                    "username": "", "display_name": "错误名称",
                    "remark": "", "nick_name": "",
                }]

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
