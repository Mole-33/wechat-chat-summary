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
            try:
                cfg_info = self.extract_master_key()
            except Exception:
                cfg_info = None
            if cfg_info:
                master, cfg_dword, current_wxid = cfg_info
                if current_wxid and current_wxid != self.wxid:
                    raise RuntimeError(
                        f"当前微信进程登录的是 {current_wxid}，请选择对应的账号目录"
                    )
                self.cfg_dword = cfg_dword
            # 微信 4.1.13+ 的 cfg 密钥字段已不能可靠派生数据库密钥；
            # Config.Cipher 扫描结果经过每个数据库页 1 的 HMAC 校验，作为主路径。
            self._keys.update(self.extract_keys())
            if not self._keys and cfg_info:
                derived = self.derive_keys_from_master(master)
                if derived:
                    self.master_key = master
                    self._keys.update(derived)
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
        def source_generation():
            src = self._db_path(rel)
            src_stat = os.stat(src)
            wal = self._wal_path(rel)
            if not wal:
                return (src_stat.st_mtime_ns, src_stat.st_size, 0, 0, b"")
            wal_stat = os.stat(wal)
            try:
                with open(wal, "rb") as handle:
                    salt = handle.read(24)[16:24]
            except OSError:
                salt = b""
            return (
                src_stat.st_mtime_ns, src_stat.st_size,
                wal_stat.st_mtime_ns, wal_stat.st_size, salt,
            )

        def generation_is_compatible(before, after):
            main_unchanged = before[:2] == after[:2]
            before_wal_size, after_wal_size = before[3], after[3]
            same_wal_generation = before[4] == after[4]
            return main_unchanged and same_wal_generation and after_wal_size >= before_wal_size

        def discard_rel_cache():
            dst = os.path.join(self.workdir, rel.replace(os.sep, "__"))
            for suffix in ("", ".stamp", "-wal", "-shm"):
                try:
                    os.remove(dst + suffix)
                except OSError:
                    pass

        last_error = None
        src = self._db_path(rel)
        dst = os.path.join(self.workdir, rel.replace(os.sep, "__"))
        stamp = dst + ".stamp"
        is_large = os.path.getsize(src) > 64 * 1024 * 1024

        # 大型库首次打开或源文件已变化时，直接走下方的一致性快照路径。
        # 避免先对活跃库完整解密数次，又因 checkpoint 世代变化全部丢弃。
        cache_current = False
        if is_large and os.path.exists(dst) and os.path.exists(stamp):
            try:
                parts = Path(stamp).read_text(encoding="ascii").split(",")
                current = source_generation()
                cache_current = (
                    int(parts[0]) == STAMP_VERSION
                    and float(parts[1]) == os.path.getmtime(src)
                    and int(parts[2]) == os.path.getsize(src)
                    and int(parts[4]) <= current[3]
                    and parts[6] == current[4].hex()
                )
            except (OSError, ValueError, IndexError):
                cache_current = False

        attempts = 1 if (not is_large or cache_current) else 0
        for _ in range(attempts):
            before = source_generation()
            try:
                conn = super()._open(rel)
            except RuntimeError as exc:
                last_error = exc
                time.sleep(0.25)
                continue
            after = source_generation()
            if generation_is_compatible(before, after):
                return conn
            conn.close()
            discard_rel_cache()
            last_error = RuntimeError(f"数据库正在 checkpoint，已自动重试: {rel}")
            time.sleep(0.15)
        if last_error is None:
            last_error = RuntimeError(f"正在创建数据库一致性快照: {rel}")
        if rel not in self._keys:
            raise last_error
        wal_path = self._wal_path(rel)
        snap_db = dst + ".encrypted-snapshot"
        snap_wal = snap_db + "-wal"
        if wal_path:
            for _ in range(3):
                try:
                    main_before = os.stat(src)
                    wal_stat_before = os.stat(wal_path)
                    with open(wal_path, "rb") as handle:
                        wal_salt_before = handle.read(24)[16:24]
                    shutil.copyfile(src, snap_db)
                    shutil.copyfile(wal_path, snap_wal)
                    main_after = os.stat(src)
                    with open(wal_path, "rb") as handle:
                        wal_salt_after = handle.read(24)[16:24]
                    snap_wal_size = os.path.getsize(snap_wal)
                    if (
                        (main_before.st_mtime_ns, main_before.st_size)
                        != (main_after.st_mtime_ns, main_after.st_size)
                        or wal_salt_before != wal_salt_after
                        or snap_wal_size < self.WAL_HEADER_SZ
                        or (snap_wal_size - self.WAL_HEADER_SZ) % self.WAL_FRAME_SZ
                    ):
                        time.sleep(0.15)
                        continue
                    self._decrypt_file(snap_db, dst, self._keys[rel])
                    applied = self._merge_wal(dst, snap_wal, self._keys[rel], 0)
                    if self._check_merged(dst):
                        src_mtime, src_size = main_before.st_mtime, main_before.st_size
                        wal_mtime, wal_size = wal_stat_before.st_mtime, snap_wal_size
                        with open(dst + ".stamp", "w", encoding="ascii") as stamp:
                            stamp.write(
                                f"{STAMP_VERSION},{src_mtime!r},{src_size},"
                                f"{wal_mtime!r},{wal_size},{applied},{wal_salt_after.hex()}"
                            )
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
        src_stat = os.stat(src)
        src_mtime, src_size = src_stat.st_mtime, src_stat.st_size
        wal_mtime, wal_size, wal_salt = 0.0, 0, ""
        if wal_path:
            try:
                wal_stat = os.stat(wal_path)
                with open(wal_path, "rb") as handle:
                    wal_salt = handle.read(24)[16:24].hex()
                wal_mtime, wal_size = wal_stat.st_mtime, wal_stat.st_size
            except OSError:
                # WeChat may checkpoint and remove the WAL between retries.
                wal_mtime, wal_size, wal_salt = 0.0, 0, ""
        with open(dst + ".stamp", "w", encoding="ascii") as stamp:
            stamp.write(
                f"{STAMP_VERSION},{src_mtime!r},{src_size},"
                f"{wal_mtime!r},{wal_size},0,{wal_salt}"
            )
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.text_factory = _sqlite_text_factory
        return conn


class WeChat4Reader:
    """Owns one selected account and deletes decrypted working copies on close."""

    def __init__(self):
        self.db: Optional[EphemeralWeChatDB] = None
        self.account = ""
        self._self_username = ""
        self._temp: Optional[tempfile.TemporaryDirectory] = None
        self._member_cache: Dict[str, Dict[str, str]] = {}
        self._member_cache_time: Dict[str, float] = {}
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
                self._self_username = str(info.get("username") or self.db.wxid)
                if not self.db._keys:
                    raise RuntimeError("未能从微信进程取得数据库解密信息，请确认微信已登录并允许读取进程")
                return {
                    "account": account,
                    "wxid": self._self_username,
                    "nickname": str(info.get("nick_name") or "").strip() or self.db.wxid,
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
            self._self_username = ""
            self._member_cache.clear()
            self._member_cache_time.clear()
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
        with self._lock:
            refreshed_at = self._member_cache_time.get(group_id, 0.0)
            if group_id not in self._member_cache or time.monotonic() - refreshed_at >= 60.0:
                db = self._require_db()
                members: Dict[str, str] = {}
                for item in db.get_group_members(group_id):
                    username = str(item.get("username") or "").strip()
                    if not username:
                        continue
                    display_name = str(item.get("display_name") or "").strip()
                    nickname = str(item.get("nick_name") or "").strip()
                    members[username] = display_name or nickname or username
                self._member_cache[group_id] = members
                self._member_cache_time[group_id] = time.monotonic()
            return self._member_cache[group_id]

    def _convert(
        self,
        group_id: str,
        group_name: str,
        row: dict,
        members: Optional[Dict[str, str]] = None,
    ) -> ChatMessage:
        db = self._require_db()
        if members is None:
            members = self._members(group_id)
        if row.get("is_self") or row.get("sender_id") in (2, "2", 3, "3"):
            sender_id = self._self_username or db.get_self_info().get("username") or db.wxid
            sender = members.get(sender_id)
            if not sender:
                info = db.get_self_info()
                sender = str(info.get("nick_name") or "").strip() or "我"
        else:
            sender_id = row.get("sender_username") or str(row.get("sender_id") or "")
            sender = members.get(sender_id)
            if not sender:
                # 纯数字 sender_id 是 message_resource 的内部 rowid，不是微信号；
                # 映射缺失时避免为每条消息重复打开 contact.db 做无效查询。
                sender = (
                    db.get_nickname(sender_id)
                    if sender_id and not sender_id.isdigit() else "未知成员"
                )
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
        progress: Optional[Callable[[int, str], None]] = None,
    ) -> List[ChatMessage]:
        if end < start:
            raise ValueError("结束时间不能早于开始时间")
        db = self._require_db()
        start_ts, end_ts = int(start.timestamp()), int(end.timestamp())
        if progress:
            progress(5, "正在打开并校验微信消息数据库")
        rows = db.get_messages_in_range(
            group_id, start_ts, end_ts,
            text_only=text_only, max_rows=max_messages + 1,
        )
        if len(rows) > max_messages:
            rows.clear()
            raise RuntimeError("所选范围超过 25 万条文字消息，请缩短时间范围")
        if progress:
            progress(70, f"已定位 {len(rows)} 条文字消息，正在解析群成员")
        messages: List[ChatMessage] = []
        members = self._members(group_id) if rows else {}
        total = max(len(rows), 1)
        for index, row in enumerate(rows, 1):
            messages.append(self._convert(group_id, group_name, row, members))
            if progress and (index == total or index % 1000 == 0):
                progress(70 + round(index / total * 30), f"正在解析聊天记录 {index}/{len(rows)}")
        rows.clear()
        return messages

    def latest_sequence(self, group_id: str) -> int:
        rows = self._require_db().get_messages(group_id, limit=1)
        return int(rows[0].get("sort_seq") or 0) if rows else 0

    def read_new(self, group_id: str, group_name: str, since_seq: int) -> dict:
        db = self._require_db()
        page_size = 500
        rows = []
        offset = 0
        # 同一轮使用固定 since_seq 并排空全部分页后才推进水位。否则当两次轮询
        # 之间累积超过 500 条（尤其边界上 sort_seq 相同）时，后页会永久漏读。
        while True:
            page = db.get_new_messages(
                group_id, since_seq=since_seq, limit=page_size, offset=offset,
            )
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        members = self._members(group_id) if rows else {}
        converted = [
            self._convert(group_id, group_name, row, members)
            for row in rows
            if row.get("type") == "文本"
        ]
        converted.sort(key=lambda item: (item.timestamp, item.id or 0))
        latest = max([since_seq] + [int(row.get("sort_seq") or 0) for row in rows])
        return {"messages": converted, "latest_seq": latest}
