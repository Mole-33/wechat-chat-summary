"""Privacy-bounded read-only adapter for Windows WeChat 4.x databases."""

from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from core.models import ChatMessage
from vendor.wechat4_db import (
    STAMP_VERSION,
    WeChatDB,
    _sqlite_text_factory,
    auto_detect_db_dir,
    list_accounts,
)


def detect_db_dir() -> Optional[str]:
    """Find xwechat_files across current and common WeChat 4.x locations."""
    candidates = [auto_detect_db_dir()]
    appdata = os.getenv("APPDATA", "")
    userprofile = os.getenv("USERPROFILE", "")
    if appdata:
        candidates.append(str(Path(appdata) / "Tencent" / "xwechat" / "xwechat_files"))
    if userprofile:
        candidates.extend([
            str(Path(userprofile) / "Documents" / "xwechat_files"),
            str(Path(userprofile) / "Documents" / "WeChat Files"),
        ])
    for candidate in candidates:
        if not candidate or not Path(candidate).is_dir():
            continue
        try:
            if any((child / "db_storage").is_dir() for child in Path(candidate).iterdir() if child.is_dir()):
                return candidate
        except OSError:
            continue
    return None


class EphemeralWeChatDB(WeChatDB):
    """WeChatDB variant that never loads or writes persistent key caches."""

    def _stable_key_file(self):
        return None

    def _save_keys(self) -> None:
        return

    def _load_or_extract_keys(self, master_key=None) -> None:
        self._keys = {}
        self.master_key = None
        self.cfg_dword = None
        if master_key:
            self._keys.update(self.derive_keys_from_master(master_key))
            self.master_key = master_key
        else:
            self._keys.update(self.extract_keys())
            try:
                derived = self.extract_master_key()
            except Exception:
                derived = None
            if derived:
                master, cfg_dword, _ = derived
                self.cfg_dword = cfg_dword
                if not self._keys:
                    self.master_key = master
                    self._keys.update(self.derive_keys_from_master(master))
        self.unkeyed = [
            rel for rel, _, _ in self._db_files
            if rel not in self._keys or not self._key_works(rel)
        ]

    def _open(self, rel: str) -> sqlite3.Connection:
        """Retry live WAL races, then fall back to a validated main-file snapshot.

        Old message shards can retain a WAL generation that is being checkpointed
        while we copy it. The encrypted main database is still a valid historical
        snapshot. Falling back keeps reads available without touching WeChat.
        """
        last_error = None
        for _ in range(1):
            try:
                return super()._open(rel)
            except RuntimeError as exc:
                last_error = exc
                time.sleep(0.25)
        if rel not in self._keys:
            raise last_error
        src = self._db_path(rel)
        dst = os.path.join(self.workdir, rel.replace(os.sep, "__"))
        wal_path = self._wal_path(rel)
        snap_db = dst + ".encrypted-snapshot"
        snap_wal = snap_db + "-wal"
        if wal_path:
            for _ in range(3):
                try:
                    shutil.copyfile(src, snap_db)
                    shutil.copyfile(wal_path, snap_wal)
                    self._decrypt_file(snap_db, dst, self._keys[rel])
                    self._merge_wal(dst, snap_wal, self._keys[rel], 0)
                    if self._check_merged(dst):
                        src_mtime, src_size = os.path.getmtime(src), os.path.getsize(src)
                        wal_mtime, wal_size = os.path.getmtime(wal_path), os.path.getsize(wal_path)
                        with open(dst + ".stamp", "w", encoding="ascii") as stamp:
                            stamp.write(f"{STAMP_VERSION},{src_mtime!r},{src_size},{wal_mtime!r},{wal_size},0")
                        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
                        conn.row_factory = sqlite3.Row
                        conn.text_factory = _sqlite_text_factory
                        return conn
                except OSError:
                    pass
                finally:
                    for snapshot in (snap_db, snap_wal):
                        try:
                            os.remove(snapshot)
                        except OSError:
                            pass
                time.sleep(0.2)
        self._decrypt_file(src, dst, self._keys[rel])
        if not self._check_merged(dst):
            raise last_error
        src_mtime, src_size = os.path.getmtime(src), os.path.getsize(src)
        wal_mtime = os.path.getmtime(wal_path) if wal_path else 0.0
        wal_size = os.path.getsize(wal_path) if wal_path else 0
        with open(dst + ".stamp", "w", encoding="ascii") as stamp:
            stamp.write(f"{STAMP_VERSION},{src_mtime!r},{src_size},{wal_mtime!r},{wal_size},0")
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.text_factory = _sqlite_text_factory
        return conn


class WeChat4Reader:
    """Owns one selected account and deletes decrypted working copies on close."""

    def __init__(self):
        self.db: Optional[EphemeralWeChatDB] = None
        self.account = ""
        self._temp: Optional[tempfile.TemporaryDirectory] = None
        self._member_cache: Dict[str, Dict[str, str]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def is_wechat_running() -> bool:
        try:
            import psutil
            return any(
                (proc.info.get("name") or "").lower() in {"weixin.exe", "wechat.exe"}
                for proc in psutil.process_iter(["name"])
            )
        except Exception:
            return False

    @staticmethod
    def accounts() -> List[dict]:
        result = []
        for item in list_accounts(db_dir=detect_db_dir()):
            result.append({
                "account": item.get("account", ""),
                "wxid": item.get("wxid", ""),
                "last_activity": item.get("last_activity", 0),
            })
        return result

    def connect(self, account: str) -> dict:
        if not account:
            raise ValueError("请选择微信账号")
        with self._lock:
            self.close()
            self._temp = tempfile.TemporaryDirectory(prefix="wechat-ai-summary-")
            workdir = str(Path(self._temp.name) / "decrypted")
            keys_file = str(Path(self._temp.name) / "disabled-keys.json")
            try:
                self.db = EphemeralWeChatDB(
                    db_dir=detect_db_dir(),
                    account=account,
                    workdir=workdir,
                    keys_file=keys_file,
                )
                self.account = account
                info = self.db.get_self_info()
                if not self.db._keys:
                    raise RuntimeError("未能从微信进程取得数据库解密信息，请确认微信已登录并允许读取进程")
                return {
                    "account": account,
                    "wxid": self.db.wxid,
                    "nickname": info.get("nick_name") or info.get("remark") or self.db.wxid,
                    "unavailable_databases": len(self.db.unkeyed),
                }
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            if self.db is not None:
                self.db._keys.clear()
                self.db.master_key = None
                self.db = None
            self.account = ""
            self._member_cache.clear()
            gc.collect()
            if self._temp is not None:
                try:
                    self._temp.cleanup()
                finally:
                    self._temp = None

    @property
    def connected(self) -> bool:
        return self.db is not None

    def _require_db(self) -> EphemeralWeChatDB:
        if self.db is None:
            raise RuntimeError("尚未连接微信数据库")
        return self.db

    def groups(self) -> List[dict]:
        db = self._require_db()
        groups = []
        for item in db.get_groups():
            groups.append({
                "id": item.get("username", ""),
                "name": item.get("name", ""),
                "member_count": item.get("member_count", 0),
            })
        groups.sort(key=lambda item: item["name"].casefold())
        return groups

    def _members(self, group_id: str) -> Dict[str, str]:
        if group_id not in self._member_cache:
            db = self._require_db()
            self._member_cache[group_id] = {
                item.get("username", ""): (
                    item.get("remark") or item.get("nick_name") or item.get("username") or "未知成员"
                )
                for item in db.get_group_members(group_id)
            }
        return self._member_cache[group_id]

    def _convert(self, group_id: str, group_name: str, row: dict) -> ChatMessage:
        db = self._require_db()
        if row.get("sender_id") == 2:
            info = db.get_self_info()
            sender = info.get("nick_name") or info.get("remark") or "我"
            sender_id = db.wxid
        else:
            sender_id = row.get("sender_username") or str(row.get("sender_id") or "")
            sender = self._members(group_id).get(sender_id)
            if not sender:
                sender = db.get_nickname(sender_id) if sender_id else "未知成员"
        return ChatMessage(
            id=int(row.get("sort_seq") or row.get("local_id") or 0),
            timestamp=datetime.fromtimestamp(int(row.get("create_time") or 0)),
            group_name=group_name,
            sender_nickname=sender,
            sender_id=sender_id,
            content=str(row.get("content") or ""),
            msg_type="text" if row.get("type") == "文本" else str(row.get("type") or "other"),
        )

    def read_range(
        self,
        group_id: str,
        group_name: str,
        start: datetime,
        end: datetime,
        text_only: bool = True,
        page_size: int = 500,
        max_messages: int = 250_000,
    ) -> List[ChatMessage]:
        if end < start:
            raise ValueError("结束时间不能早于开始时间")
        db = self._require_db()
        start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
        offset = 0
        messages: List[ChatMessage] = []
        reached_start = False
        while not reached_start:
            rows = db.get_messages(group_id, limit=page_size, offset=offset)
            if not rows:
                break
            for row in rows:
                timestamp = int(row.get("create_time") or 0)
                if timestamp > end_ts:
                    continue
                if timestamp < start_ts:
                    reached_start = True
                    continue
                if text_only and row.get("type") != "文本":
                    continue
                messages.append(self._convert(group_id, group_name, row))
                if len(messages) > max_messages:
                    raise RuntimeError("所选范围超过 25 万条文字消息，请缩短时间范围")
            offset += len(rows)
            if len(rows) < page_size:
                break
        messages.sort(key=lambda item: (item.timestamp, item.id or 0))
        return messages

    def latest_sequence(self, group_id: str) -> int:
        rows = self._require_db().get_messages(group_id, limit=1)
        return int(rows[0].get("sort_seq") or 0) if rows else 0

    def read_new(self, group_id: str, group_name: str, since_seq: int) -> dict:
        rows = self._require_db().get_new_messages(group_id, since_seq=since_seq, limit=500)
        converted = [
            self._convert(group_id, group_name, row)
            for row in rows
            if row.get("type") == "文本"
        ]
        converted.sort(key=lambda item: (item.timestamp, item.id or 0))
        latest = max([since_seq] + [int(row.get("sort_seq") or 0) for row in rows])
        return {"messages": converted, "latest_seq": latest}
