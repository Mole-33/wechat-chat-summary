"""Persistent aggregate statistics. Message bodies are never stored here."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from config import DB_PATH
from core.models import GroupDailySummary, MemberDailyStat


class StorageManager:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_group_stats (
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    total_messages INTEGER NOT NULL,
                    total_members INTEGER NOT NULL,
                    total_words INTEGER NOT NULL,
                    peak_hour INTEGER,
                    peak_hour_count INTEGER NOT NULL,
                    refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, date_str)
                );
                CREATE TABLE IF NOT EXISTS daily_member_stats (
                    group_id TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    total_words INTEGER NOT NULL,
                    first_time TEXT,
                    last_time TEXT,
                    rank_no INTEGER NOT NULL,
                    PRIMARY KEY (group_id, date_str, nickname)
                );
                CREATE TABLE IF NOT EXISTS daily_hour_stats (
                    group_id TEXT NOT NULL,
                    date_str TEXT NOT NULL,
                    hour_no INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    PRIMARY KEY (group_id, date_str, hour_no)
                );
                CREATE TABLE IF NOT EXISTS app_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                );
                """
            )
            conn.commit()

    def replace_daily_summary(self, group_id: str, summary: GroupDailySummary) -> None:
        """Replace one day's aggregates without persisting any message content."""
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO daily_group_stats
                (group_id, group_name, date_str, total_messages, total_members,
                 total_words, peak_hour, peak_hour_count, refreshed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(group_id, date_str) DO UPDATE SET
                  group_name=excluded.group_name,
                  total_messages=excluded.total_messages,
                  total_members=excluded.total_members,
                  total_words=excluded.total_words,
                  peak_hour=excluded.peak_hour,
                  peak_hour_count=excluded.peak_hour_count,
                  refreshed_at=CURRENT_TIMESTAMP""",
                (
                    group_id, summary.group_name, summary.date_str,
                    summary.total_messages, summary.total_members_spoke,
                    summary.total_words, summary.peak_hour, summary.peak_hour_count,
                ),
            )
            conn.execute(
                "DELETE FROM daily_member_stats WHERE group_id=? AND date_str=?",
                (group_id, summary.date_str),
            )
            conn.executemany(
                """INSERT INTO daily_member_stats
                (group_id, date_str, nickname, message_count, total_words,
                 first_time, last_time, rank_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        group_id, summary.date_str, stat.nickname,
                        stat.message_count, stat.total_words,
                        stat.first_msg_time, stat.last_msg_time, stat.rank,
                    )
                    for stat in summary.member_stats
                ],
            )
            conn.execute(
                "DELETE FROM daily_hour_stats WHERE group_id=? AND date_str=?",
                (group_id, summary.date_str),
            )
            conn.executemany(
                "INSERT INTO daily_hour_stats(group_id,date_str,hour_no,message_count) VALUES(?,?,?,?)",
                [(group_id, summary.date_str, hour, count) for hour, count in summary.hourly_distribution.items()],
            )
            conn.commit()

    def get_daily_summary(self, group_id: str, date_str: str) -> Optional[GroupDailySummary]:
        with self._get_connection() as conn:
            group = conn.execute(
                "SELECT * FROM daily_group_stats WHERE group_id=? AND date_str=?",
                (group_id, date_str),
            ).fetchone()
            if not group:
                return None
            members = conn.execute(
                """SELECT * FROM daily_member_stats WHERE group_id=? AND date_str=?
                ORDER BY rank_no""",
                (group_id, date_str),
            ).fetchall()
            hours = conn.execute(
                "SELECT hour_no,message_count FROM daily_hour_stats WHERE group_id=? AND date_str=?",
                (group_id, date_str),
            ).fetchall()
        stats = []
        total = int(group["total_messages"])
        for row in members:
            stats.append(MemberDailyStat(
                nickname=row["nickname"],
                message_count=row["message_count"],
                total_words=row["total_words"],
                ratio=(row["message_count"] / total) if total else 0.0,
                first_msg_time=row["first_time"],
                last_msg_time=row["last_time"],
                rank=row["rank_no"],
            ))
        hourly = {hour: 0 for hour in range(24)}
        hourly.update({row["hour_no"]: row["message_count"] for row in hours})
        return GroupDailySummary(
            group_name=group["group_name"],
            date_str=date_str,
            total_messages=total,
            total_members_spoke=group["total_members"],
            total_words=group["total_words"],
            peak_hour=group["peak_hour"],
            peak_hour_count=group["peak_hour_count"],
            member_stats=stats,
            hourly_distribution=hourly,
        )

    def get_all_groups(self) -> List[Dict[str, str]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT group_id, group_name, MAX(date_str) AS last_date
                FROM daily_group_stats GROUP BY group_id, group_name
                ORDER BY group_name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_available_dates(self, group_id: str) -> List[str]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT date_str FROM daily_group_stats WHERE group_id=? ORDER BY date_str DESC",
                (group_id,),
            ).fetchall()
        return [row["date_str"] for row in rows]

    def set_meta(self, key: str, value) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO app_meta(meta_key,meta_value) VALUES(?,?)
                ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value""",
                (key, encoded),
            )
            conn.commit()

    def get_meta(self, key: str, default=None):
        with self._get_connection() as conn:
            row = conn.execute("SELECT meta_value FROM app_meta WHERE meta_key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["meta_value"])
        except json.JSONDecodeError:
            return default

