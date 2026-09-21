"""Local-only HTTP API and static dashboard server."""

from __future__ import annotations

import json
import ctypes
import logging
import mimetypes
import os
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import APP_NAME, APP_VERSION, DEFAULT_CONFIG, EXPORTS_DIR, REPORTS_DIR
from core.ai_client import APIClientError, CompatibleAIClient
from core.notifications import notify
from core.reporter import ReportGenerator
from core.secure_settings import SettingsStore
from core.stats_engine import StatsEngine
from core.storage import StorageManager
from core.summarizer import summarize_messages, summary_to_markdown
from core.updater import UpdateManager
from listeners.wechat4_reader import WeChat4Reader


LOGGER = logging.getLogger("wechat_ai_summary")


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    STATIC_DIR = Path(sys._MEIPASS) / "gui" / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field}格式无效") from None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _summary_json(summary, self_nickname: str = "") -> Dict[str, Any]:
    if summary is None:
        return {}
    leaderboard = [
        {
            "rank": item.rank,
            "nickname": item.nickname,
            "count": item.message_count,
            "ratio": round(item.ratio * 100, 1),
            "words": item.total_words,
            "avg_words": item.avg_words_per_msg,
            "first_time": item.first_msg_time or "",
            "last_time": item.last_msg_time or "",
            "is_self": bool(self_nickname and item.nickname == self_nickname),
        }
        for item in summary.member_stats
    ]
    return {
        "group_name": summary.group_name,
        "date": summary.date_str,
        "total_messages": summary.total_messages,
        "total_members": summary.total_members_spoke,
        "total_words": summary.total_words,
        "peak_hour": summary.peak_hour,
        "peak_hour_count": summary.peak_hour_count,
        "leaderboard": leaderboard,
        "hourly_distribution": [summary.hourly_distribution.get(hour, 0) for hour in range(24)],
    }


class GUIStateManager:
    def __init__(self):
        self.settings = SettingsStore()
        self.storage = StorageManager()
        self.reporter = ReportGenerator()
        self.engine = StatsEngine(filter_system_messages=True)
        self.reader = WeChat4Reader()
        self.account_info: Dict[str, Any] = {}
        self.groups: Dict[str, Dict[str, Any]] = {}
        self.live_messages: List[Dict[str, Any]] = []
        self.monitor_watermarks: Dict[str, int] = {}
        self.monitor_failures: Dict[str, int] = {}
        self.monitor_error = ""
        self.is_monitoring = False
        self.should_exit = False
        self.startup_state = "idle"
        self.startup_message = ""
        self._startup_restore_started = False
        self._startup_restore_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._db_lock = threading.RLock()
        self.summary_jobs: Dict[str, Dict[str, Any]] = {}
        self._summary_job_lock = threading.RLock()
        self._current_summary_job_id = ""
        self.updater = UpdateManager()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def status(self) -> Dict[str, Any]:
        selected = self.settings.get("selected_groups", [])
        return {
            "app_name": APP_NAME,
            "version": APP_VERSION,
            "wechat_running": self.reader.is_wechat_running(),
            "connected": self.reader.connected,
            "account": self.account_info,
            "is_monitoring": self.is_monitoring,
            "monitor_error": self.monitor_error,
            "startup_state": self.startup_state,
            "startup_message": self.startup_message,
            "selected_groups": selected,
            "live_message_count": len(self.live_messages),
            "schedule_enabled": bool(self.settings.get("schedule_enabled", False)),
            "active_summary_job_id": self.active_summary_job_id(),
            "latest_summary_job_id": self.latest_summary_job_id(),
        }

    def _ensure_summary_job_state(self) -> None:
        if not hasattr(self, "summary_jobs"):
            self.summary_jobs = {}
        if not hasattr(self, "_summary_job_lock"):
            self._summary_job_lock = threading.RLock()
        if not hasattr(self, "_current_summary_job_id"):
            self._current_summary_job_id = ""

    def active_summary_job_id(self) -> str:
        self._ensure_summary_job_state()
        with self._summary_job_lock:
            job = self.summary_jobs.get(self._current_summary_job_id)
            if job and job.get("status") in {"queued", "running"}:
                return self._current_summary_job_id
            self._current_summary_job_id = ""
            return ""

    def latest_summary_job_id(self) -> str:
        self._ensure_summary_job_state()
        with self._summary_job_lock:
            if not self.summary_jobs:
                return ""
            return max(
                self.summary_jobs,
                key=lambda job_id: self.summary_jobs[job_id].get("created_at") or "",
            )

    def _prune_summary_jobs(self, keep: int = 20) -> None:
        finished = sorted(
            (
                job_id for job_id, job in self.summary_jobs.items()
                if job.get("status") not in {"queued", "running"}
            ),
            key=lambda job_id: self.summary_jobs[job_id].get("finished_at")
            or self.summary_jobs[job_id].get("created_at") or "",
        )
        while len(self.summary_jobs) >= keep and finished:
            self.summary_jobs.pop(finished.pop(0), None)

    def restore_saved_session_async(self) -> bool:
        """Reconnect the saved account and resume live reads once per launch."""
        account = str(self.settings.get("selected_account", "") or "").strip()
        selected = list(self.settings.get("selected_groups", []) or [])
        if not account or not selected:
            self.startup_state = "idle"
            self.startup_message = ""
            return False
        with self._lock:
            if self._startup_restore_started:
                return False
            self._startup_restore_started = True
            self.startup_state = "connecting"
            self.startup_message = "正在自动连接微信并开启实时读取"
            self._startup_restore_thread = threading.Thread(
                target=self._restore_saved_session_worker,
                args=(account, selected),
                daemon=True,
            )
            self._startup_restore_thread.start()
        return True

    def _restore_saved_session_worker(self, account: str, selected: List[Dict[str, str]]) -> None:
        try:
            if self.should_exit:
                return
            self.connect(account)
            available = {
                item["id"]: {"id": item["id"], "name": item["name"]}
                for item in self.groups.values()
                if item.get("id")
            }
            restored = [available[item.get("id")] for item in selected if item.get("id") in available]
            if not restored:
                raise RuntimeError("上次选择的群聊已不存在，请重新选择群聊")
            if restored != selected:
                self.settings.set("selected_groups", restored)
            if self.should_exit:
                return
            result = self.start_monitoring()
            self.startup_state = "monitoring"
            self.startup_message = result.get("message", "实时读取已自动开启")
        except Exception as exc:
            self.startup_state = "error"
            self.startup_message = f"自动开启实时读取失败：{exc}"

    def connect(self, account: str) -> Dict[str, Any]:
        self.stop_monitoring()
        try:
            with self._db_lock:
                info = self.reader.connect(account)
                groups = self.reader.groups()
            if not groups:
                raise RuntimeError(
                    "已读取账号数据库，但没有找到群聊。请确认微信中至少有一个群聊；"
                    "若群聊确实存在，请发送日志目录中的 app.log 以便继续兼容。"
                )
        except Exception:
            with self._db_lock:
                self.reader.close()
            self.account_info = {}
            self.groups = {}
            raise
        self.account_info = info
        self.groups = {item["id"]: item for item in groups}
        self.settings.set("selected_account", account)
        return {"success": True, "account": info, "groups": groups}

    def disconnect(self) -> None:
        self.stop_monitoring()
        with self._db_lock:
            self.reader.close()
        self.account_info = {}
        self.groups = {}
        self.live_messages.clear()

    def select_groups(self, groups: List[Dict[str, str]]) -> None:
        clean = []
        for item in groups:
            group_id = str(item.get("id") or "")
            if group_id in self.groups:
                clean.append({"id": group_id, "name": self.groups[group_id]["name"]})
        # 实时读取期间新增群聊时，从它的当前末尾开始监听，不能用默认水位 0
        # 把整个历史记录误当成实时消息。先准备水位，再发布新的群聊选择，避免
        # 轮询线程在两步之间看见尚未初始化的新群。
        if self.is_monitoring:
            selected_ids = {item["id"] for item in clean}
            with self._lock:
                watermarks = {
                    group_id: seq for group_id, seq in self.monitor_watermarks.items()
                    if group_id in selected_ids
                }
            with self._db_lock:
                for item in clean:
                    if item["id"] not in watermarks:
                        watermarks[item["id"]] = self.reader.latest_sequence(item["id"])
            with self._lock:
                self.monitor_watermarks = watermarks
        self.settings.set("selected_groups", clean)

    def _group(self, group_id: str) -> Dict[str, Any]:
        item = self.groups.get(group_id)
        if item:
            return item
        for saved in self.settings.get("selected_groups", []):
            if saved.get("id") == group_id:
                return saved
        raise ValueError("未找到所选群聊，请重新连接微信并选择群聊")

    def start_monitoring(self) -> Dict[str, Any]:
        if not self.reader.connected:
            raise RuntimeError("请先连接微信账号")
        selected = self.settings.get("selected_groups", [])
        if not selected:
            raise ValueError("请至少选择一个群聊")
        # 所有需要同时碰数据库锁与状态锁的路径统一按 db -> state 顺序，
        # 避免保存群聊和启动监听并发时互相等待。
        with self._db_lock:
            with self._lock:
                if self.is_monitoring:
                    return {"success": True, "message": "实时读取已在运行"}
            watermarks = {
                group["id"]: self.reader.latest_sequence(group["id"])
                for group in selected
            }
            with self._lock:
                # 另一个并发请求可能已在数据库扫描期间启动了监听。
                if self.is_monitoring:
                    return {"success": True, "message": "实时读取已在运行"}
                self.monitor_watermarks = watermarks
                self.monitor_failures = {}
                self.monitor_error = ""
                # 每个轮询线程使用独立停止事件。旧线程即使正在慢查询，也不会因为
                # 新一轮 clear() 而被意外复活并与新线程重复读取。
                stop_event = threading.Event()
                self._stop_monitor = stop_event
                self.is_monitoring = True
                self._monitor_thread = threading.Thread(
                    target=self._monitor_loop, args=(stop_event,), daemon=True,
                )
                self._monitor_thread.start()
        return {"success": True, "message": f"已开始读取 {len(selected)} 个群的新文字消息"}

    def stop_monitoring(self) -> Dict[str, Any]:
        with self._lock:
            self._stop_monitor.set()
            self.is_monitoring = False
        return {"success": True, "message": "实时读取已停止"}

    def _monitor_loop(self, stop_event: Optional[threading.Event] = None) -> None:
        event = stop_event or self._stop_monitor
        current_thread = threading.current_thread()
        try:
            while not event.wait(float(DEFAULT_CONFIG["poll_interval_seconds"])):
                selected = list(self.settings.get("selected_groups", []))
                for group in selected:
                    if event.is_set():
                        break
                    group_id, name = group["id"], group["name"]
                    with self._lock:
                        # 群聊可能刚在运行中被取消；不要再写入它的消息。
                        if group_id not in self.monitor_watermarks:
                            continue
                        since = self.monitor_watermarks[group_id]
                    try:
                        with self._db_lock:
                            batch = self.reader.read_new(group_id, name, since)
                        with self._lock:
                            if event.is_set() or group_id not in self.monitor_watermarks:
                                continue
                            self.monitor_failures.pop(group_id, None)
                            if not self.monitor_failures:
                                self.monitor_error = ""
                            self.monitor_watermarks[group_id] = int(batch["latest_seq"])
                        if not batch["messages"]:
                            continue
                        with self._lock:
                            for message in batch["messages"]:
                                self.live_messages.append({
                                    "time": message.timestamp.strftime("%H:%M:%S"),
                                    "timestamp": message.timestamp.isoformat(timespec="seconds"),
                                    "sender": message.sender_nickname,
                                    "content": message.content,
                                    "group": name,
                                    "is_self": message.sender_id == self.account_info.get("wxid"),
                                })
                            # 多个群按顺序读取，但看板必须按真实消息时间全局排序，
                            # 不能让“后读取的群”整批盖到较新的消息上面。
                            self.live_messages.sort(
                                key=lambda item: item.get("timestamp", ""), reverse=True,
                            )
                            del self.live_messages[500:]
                    except Exception as exc:
                        with self._lock:
                            failures = self.monitor_failures.get(group_id, 0) + 1
                            self.monitor_failures[group_id] = failures
                        if failures in {1, 3}:
                            LOGGER.warning("实时读取【%s】连续失败 %s 次：%s", name, failures, exc)
                        if failures >= 3:
                            with self._lock:
                                self.monitor_error = f"【{name}】连续读取失败：{exc}"
                        continue
        finally:
            with self._lock:
                # 退出的旧线程不能把后来新线程的运行状态改成“已停止”。
                if self._monitor_thread is current_thread:
                    self.is_monitoring = False
                    self._monitor_thread = None

    def refresh_stats(self, group_id: str, day: str) -> Dict[str, Any]:
        group = self._group(group_id)
        target = date.fromisoformat(day)
        start = datetime.combine(target, dt_time.min)
        end = datetime.combine(target, dt_time.max)
        if self.reader.connected:
            with self._db_lock:
                messages = self.reader.read_range(group_id, group["name"], start, end)
            summary = self.engine.compute_daily_summary(messages, group["name"], day)
            self.storage.replace_daily_summary(group_id, summary)
            messages.clear()
        else:
            summary = self.storage.get_daily_summary(group_id, day)
        if summary is None:
            return {
                "group_name": group["name"], "date": day, "total_messages": 0,
                "total_members": 0, "total_words": 0, "peak_hour": None,
                "peak_hour_count": 0, "leaderboard": [], "hourly_distribution": [0] * 24,
            }
        return _summary_json(summary, self.account_info.get("nickname", ""))

    def _client(self, provider_id: str, require_model: bool = False) -> CompatibleAIClient:
        profile = self.settings.provider_with_secret(provider_id)
        if not profile:
            raise ValueError("请选择有效的 AI 平台配置")
        if not str(profile.get("api_key") or "").strip():
            raise ValueError("该平台尚未保存 API Key，请先打开“AI 与代理设置”填写并保存")
        if require_model and not str(profile.get("model") or "").strip():
            raise ValueError("该平台尚未选择模型，请先自动获取或手动填写模型名称并保存")
        return CompatibleAIClient(
            profile,
            proxy_url=self.settings.get("proxy_url", ""),
            proxy_username=self.settings.get("proxy_username", ""),
            proxy_password=profile.get("proxy_password", ""),
        )

    def start_summary_job(
        self, group_ids: List[str], start: datetime, end: datetime,
        provider_id: str, automatic: bool = False,
    ) -> str:
        group_ids = list(dict.fromkeys(group_ids))
        if not group_ids:
            raise ValueError("请至少选择一个群聊")
        if not self.reader.connected:
            raise RuntimeError("请先连接微信账号")
        self._ensure_summary_job_state()
        if self.active_summary_job_id():
            raise ValueError("已有总结任务正在运行，请等待当前任务完成")
        client = self._client(provider_id, require_model=True)
        job_id = uuid.uuid4().hex
        with self._summary_job_lock:
            if self.active_summary_job_id():
                raise ValueError("已有总结任务正在运行，请等待当前任务完成")
            self._prune_summary_jobs()
            self.summary_jobs[job_id] = {
                "id": job_id, "status": "queued", "progress": 0,
                "message": "任务已创建，准备读取聊天记录", "results": {}, "errors": {},
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "automatic": automatic,
            }
            self._current_summary_job_id = job_id

        def worker_body():
            job = self.summary_jobs[job_id]
            job["status"] = "running"
            provider_kind = getattr(client, "profile", {}).get("kind")
            if provider_kind in {"siliconflow", "minimax"}:
                provider_name = "MiniMax" if provider_kind == "minimax" else "硅基流动"
                job["progress"] = 1
                job["message"] = f"正在检查 {provider_name} API、Key 与网络连接"
                try:
                    client.list_models()
                except Exception as exc:
                    for group_id in group_ids:
                        job["errors"][group_id] = str(exc)
                    job["status"] = "failed"
                    job["progress"] = 100
                    job["message"] = "AI 平台连接检查失败，尚未读取微信消息"
                    job["finished_at"] = datetime.now().isoformat(timespec="seconds")
                    notify("群聊总结失败", f"{provider_name} 连接检查失败：{exc}")
                    return
            completed = 0
            for group_id in group_ids:
                messages = []
                try:
                    group = self._group(group_id)
                    group_number = completed + 1
                    group_total = len(group_ids)
                    job["progress"] = round(completed / group_total * 100)
                    job["message"] = f"正在读取【{group['name']}】聊天记录（{group_number}/{group_total} 群）"

                    def read_progress(percent, text):
                        group_base = completed / group_total
                        read_part = max(0, min(percent / 100, 1)) * 0.25 / group_total
                        job["progress"] = round((group_base + read_part) * 100)
                        job["message"] = f"【{group['name']}】{text}（{group_number}/{group_total} 群）"

                    with self._db_lock:
                        messages = self.reader.read_range(
                            group_id, group["name"], start, end, progress=read_progress,
                        )
                    job["progress"] = round((completed + 0.25) / group_total * 100)
                    job["message"] = f"【{group['name']}】已读取 {len(messages)} 条，准备调用 AI"
                    def progress(call_index, total_calls, text):
                        group_base = completed / len(group_ids)
                        call_ratio = max(0, min(call_index / max(total_calls, 1), 1))
                        group_part = (0.25 + call_ratio * 0.70) / len(group_ids)
                        job["progress"] = round((group_base + group_part) * 100)
                        job["message"] = f"【{group['name']}】{text}（{group_number}/{group_total} 群）"

                    result = summarize_messages(client, group["name"], start, end, messages, progress)
                    job["results"][group_id] = {
                        "summary": result,
                        "markdown": summary_to_markdown(result),
                    }
                    if automatic:
                        self.storage.set_meta(f"last_summary_success:{group_id}", end.isoformat())
                except Exception as exc:
                    job["errors"][group_id] = str(exc)
                finally:
                    messages.clear()
                completed += 1
                job["progress"] = round(completed / len(group_ids) * 100)
            job["status"] = "completed" if job["results"] else "failed"
            job["message"] = "总结完成" if job["results"] else "总结失败"
            job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            notify(
                "群聊总结完成" if job["results"] else "群聊总结失败",
                f"成功 {len(job['results'])} 个群，失败 {len(job['errors'])} 个群",
            )

        def worker():
            try:
                worker_body()
            except Exception as exc:
                LOGGER.exception("总结任务意外失败：%s", job_id)
                job = self.summary_jobs[job_id]
                job["errors"]["_global"] = str(exc)
                job["status"] = "failed"
                job["progress"] = 100
                job["message"] = "总结任务意外失败"
                job["finished_at"] = datetime.now().isoformat(timespec="seconds")
            finally:
                with self._summary_job_lock:
                    if self._current_summary_job_id == job_id:
                        self._current_summary_job_id = ""

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def _scheduler_loop(self) -> None:
        while not self.should_exit:
            time.sleep(15)
            try:
                self._scheduler_tick()
            except Exception:
                continue

    def _scheduler_tick(self) -> None:
        if not self.settings.get("schedule_enabled", False) or not self.reader.connected:
            return
        now = datetime.now()
        if now.strftime("%H:%M") != self.settings.get("daily_summary_time", "23:30"):
            return
        marker = self.storage.get_meta("schedule_last_fired_day", "")
        if marker == now.strftime("%Y-%m-%d"):
            return
        provider_id = self.settings.get("active_provider_id", "")
        groups = self.settings.get("selected_groups", [])
        first_backfill = self.settings.get("first_backfill", {})
        runnable = []
        for group in groups:
            group_id = group["id"]
            raw = self.storage.get_meta(f"last_summary_success:{group_id}") or first_backfill.get(group_id)
            if not raw:
                continue
            runnable.append((group_id, _parse_datetime(raw, "首次回溯时间")))
        if not runnable or not provider_id:
            return
        self.storage.set_meta("schedule_last_fired_day", now.strftime("%Y-%m-%d"))
        def dispatch_scheduled_jobs():
            for group_id, start in runnable:
                if self.should_exit:
                    return
                while self.active_summary_job_id() and not self.should_exit:
                    time.sleep(1)
                if self.should_exit:
                    return
                try:
                    job_id = self.start_summary_job([group_id], start, now, provider_id, automatic=True)
                except Exception as exc:
                    LOGGER.exception("定时总结启动失败：%s", exc)
                    continue
                while self.summary_jobs.get(job_id, {}).get("status") in {"queued", "running"}:
                    if self.should_exit:
                        return
                    time.sleep(1)

        threading.Thread(target=dispatch_scheduled_jobs, daemon=True).start()

    def cleanup(self) -> None:
        self.should_exit = True
        self.stop_monitoring()
        self.disconnect()
        with self._lock:
            self.live_messages.clear()
        with self._summary_job_lock:
            self.summary_jobs.clear()
            self._current_summary_job_id = ""


state = GUIStateManager()


class AppHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = f"WeChatAISummary/{APP_VERSION}"

    def log_message(self, format, *args):
        return

    def _json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def _error(self, exc: Exception):
        LOGGER.exception("本地接口处理失败：%s", self.path)
        code = 400 if isinstance(exc, (ValueError, APIClientError)) else 500
        self._json({"success": False, "message": str(exc)}, code)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/status":
                return self._json(state.status())
            if path == "/api/accounts":
                return self._json({"accounts": state.reader.accounts()})
            if path == "/api/groups":
                return self._json({"groups": list(state.groups.values())})
            if path == "/api/settings":
                return self._json(state.settings.public_settings())
            if path == "/api/messages/live":
                with state._lock:
                    return self._json({"messages": state.live_messages[:200]})
            if path == "/api/stats/today":
                group_id = query.get("group_id", [""])[0]
                day = query.get("date", [date.today().isoformat()])[0]
                return self._json(state.refresh_stats(group_id, day))
            if path == "/api/summary/job":
                job_id = query.get("id", [""])[0]
                job = state.summary_jobs.get(job_id)
                if not job:
                    raise ValueError("未找到总结任务")
                return self._json(job)
            if path == "/api/summary/download":
                job_id = query.get("id", [""])[0]
                group_id = query.get("group_id", [""])[0]
                fmt = query.get("format", ["md"])[0]
                job = state.summary_jobs.get(job_id, {})
                result = job.get("results", {}).get(group_id)
                if not result:
                    raise ValueError("未找到可导出的总结")
                text = result["markdown"]
                if fmt == "txt":
                    text = text.replace("# ", "").replace("## ", "")
                body = text.encode("utf-8")
                safe = "群聊总结." + ("txt" if fmt == "txt" else "md")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(safe)}")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/heartbeat":
                return self._json({"status": "ok"})

            file_path = STATIC_DIR / ("index.html" if path in ("", "/") else path.lstrip("/"))
            if file_path.exists() and file_path.is_file() and STATIC_DIR in file_path.resolve().parents:
                content = file_path.read_bytes()
                mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", f"{mime}; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_error(404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = self._payload()
            if path == "/api/wechat/connect":
                return self._json(state.connect(str(payload.get("account") or "")))
            if path == "/api/wechat/disconnect":
                state.disconnect()
                return self._json({"success": True})
            if path == "/api/wechat/relaunch-admin":
                if not getattr(sys, "frozen", False):
                    raise ValueError("源码运行时请手动以管理员身份启动终端")
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, "", str(Path(sys.executable).parent), 1
                )
                if result <= 32:
                    raise RuntimeError("未能请求管理员权限")
                state.should_exit = True
                return self._json({"success": True, "message": "正在以管理员身份重新启动"})
            if path == "/api/groups/select":
                state.select_groups(payload.get("groups") or [])
                return self._json({"success": True})
            if path == "/api/monitor/start":
                return self._json(state.start_monitoring())
            if path == "/api/monitor/stop":
                return self._json(state.stop_monitoring())
            if path == "/api/providers/save":
                return self._json({"success": True, "provider": state.settings.upsert_provider(payload)})
            if path == "/api/providers/delete":
                state.settings.delete_provider(str(payload.get("id") or ""))
                return self._json({"success": True})
            if path == "/api/providers/models":
                models = state._client(str(payload.get("id") or "")).list_models()
                return self._json({"success": True, "models": models})
            if path == "/api/providers/test":
                client = state._client(str(payload.get("id") or ""))
                if client.profile.get("model"):
                    response = client.chat("你是连接测试助手。", "只回复：连接成功")
                    return self._json({"success": True, "message": response["content"][:100]})
                models = client.list_models()
                return self._json({"success": True, "message": f"连接成功，可用模型 {len(models)} 个"})
            if path == "/api/proxy/save":
                state.settings.set_proxy(payload.get("url", ""), payload.get("username", ""), payload.get("password"))
                return self._json({"success": True})
            if path == "/api/update/check":
                return self._json(state.updater.check())
            if path == "/api/update/stage":
                checked = state.updater.check()
                if not checked.get("available"):
                    return self._json(checked)
                staged = state.updater.stage(checked["asset"])
                return self._json({"success": True, "path": str(staged), "message": "更新包已验证并下载；退出应用后解压覆盖即可，当前版本可作为回滚副本"})
            if path == "/api/summary/run":
                start = _parse_datetime(payload.get("start"), "开始时间")
                end = _parse_datetime(payload.get("end"), "结束时间")
                if end <= start:
                    raise ValueError("结束时间必须晚于开始时间")
                job_id = state.start_summary_job(
                    payload.get("group_ids") or [], start, end,
                    str(payload.get("provider_id") or ""), automatic=False,
                )
                return self._json({"success": True, "job_id": job_id})
            if path == "/api/settings/schedule":
                value = str(payload.get("time") or "23:30")
                datetime.strptime(value, "%H:%M")
                state.settings.set("daily_summary_time", value)
                state.settings.set("schedule_enabled", bool(payload.get("enabled")))
                state.settings.set("active_provider_id", str(payload.get("provider_id") or ""))
                state.settings.set("first_backfill", payload.get("first_backfill") or {})
                return self._json({"success": True})
            if path == "/api/report/excel":
                group_id = str(payload.get("group_id") or "")
                day = str(payload.get("date") or date.today().isoformat())
                group = state._group(group_id)
                target = date.fromisoformat(day)
                start, end = datetime.combine(target, dt_time.min), datetime.combine(target, dt_time.max)
                with state._db_lock:
                    messages = state.reader.read_range(group_id, group["name"], start, end)
                if not messages:
                    raise ValueError("该日期没有文字消息")
                summary = state.engine.compute_daily_summary(messages, group["name"], day)
                path_out = state.reporter.export_to_excel(summary, messages)
                messages.clear()
                try:
                    os.startfile(path_out)
                except OSError:
                    pass
                return self._json({"success": True, "path": str(path_out)})
            if path == "/api/report/open-folder":
                os.startfile(REPORTS_DIR.resolve())
                return self._json({"success": True})
            if path == "/api/shutdown":
                state.should_exit = True
                return self._json({"success": True})
            self.send_error(404)
        except Exception as exc:
            self._error(exc)


def run_gui_server(port: int = 18989, restore_saved_session: bool = True):
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHTTPRequestHandler)
    if restore_saved_session:
        state.restore_saved_session_async()
    server.serve_forever()


if __name__ == "__main__":
    run_gui_server()
