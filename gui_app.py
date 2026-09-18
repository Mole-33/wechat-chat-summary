"""Windows launcher for the local dashboard."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from config import APP_NAME, APP_VERSION, LOGS_DIR


LOGGER = logging.getLogger("wechat_ai_summary")
PORT_START = 18989
PORT_END = 19038


def configure_logging() -> Path:
    log_path = LOGS_DIR / "app.log"
    if not LOGGER.handlers:
        LOGGER.setLevel(logging.INFO)
        handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)
    return log_path


def show_error(message: str, log_path: Path | None = None) -> None:
    detail = message
    if log_path:
        detail += f"\n\n诊断日志：{log_path}"
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, detail, f"{APP_NAME} - 启动失败", 0x10)
    else:
        print(detail, file=sys.stderr)


def find_free_port(start_port: int = PORT_START) -> int:
    for port in range(start_port, PORT_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("本地端口 18989–19038 均被占用，请关闭占用端口的程序后重试")


def find_existing_instance() -> str:
    for port in range(PORT_START, PORT_END + 1):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("app_name") == APP_NAME:
                return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return ""


def open_existing_dashboard(url: str) -> None:
    LOGGER.info("检测到已运行实例：%s", url)
    if not webbrowser.open(url, new=2):
        show_error(f"应用已在后台运行，请在浏览器中打开：\n{url}")


def launch_default_browser(url: str) -> bool:
    """Open the dashboard with the user's Windows default browser."""
    if os.getenv("WECHAT_AI_SUMMARY_NO_BROWSER") == "1":
        LOGGER.info("测试模式：跳过默认浏览器")
        return True
    opened = bool(webbrowser.open(url, new=2))
    if opened:
        LOGGER.info("已使用系统默认浏览器打开：%s", url)
    else:
        LOGGER.warning("系统未能自动打开默认浏览器：%s", url)
    return opened


def _create_single_instance_mutex():
    if sys.platform != "win32":
        return None, False
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\WeChatAISummary-0E5F9A31")
    already_exists = kernel32.GetLastError() == 183
    return handle, already_exists


def run() -> None:
    log_path = configure_logging()
    LOGGER.info("启动 %s v%s，PID=%s", APP_NAME, APP_VERSION, os.getpid())

    existing = find_existing_instance()
    if existing:
        open_existing_dashboard(existing)
        return

    mutex_handle, already_exists = _create_single_instance_mutex()
    if already_exists:
        for _ in range(20):
            time.sleep(0.25)
            existing = find_existing_instance()
            if existing:
                open_existing_dashboard(existing)
                return
        raise RuntimeError("另一个实例正在启动，请稍候再试")

    from http.server import ThreadingHTTPServer
    from gui.server import AppHTTPRequestHandler, state

    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHTTPRequestHandler)
    url = f"http://127.0.0.1:{port}"
    import threading
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    LOGGER.info("本地服务已启动：%s", url)

    try:
        if not launch_default_browser(url):
            show_error(f"无法自动打开默认浏览器，请手动访问：\n{url}", log_path)
        while not state.should_exit:
            time.sleep(0.5)
    finally:
        state.cleanup()
        server.shutdown()
        server.server_close()
        if mutex_handle and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            ctypes.windll.kernel32.CloseHandle.restype = wintypes.BOOL
            ctypes.windll.kernel32.CloseHandle(mutex_handle)
        LOGGER.info("应用已安全退出")


def main() -> None:
    log_path = None
    try:
        log_path = configure_logging()
        run()
    except Exception as exc:
        try:
            LOGGER.exception("启动失败")
        except Exception:
            traceback.print_exc()
        show_error(f"应用启动失败：{exc}", log_path)


if __name__ == "__main__":
    main()
