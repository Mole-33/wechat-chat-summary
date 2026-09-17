"""Windows launcher for the local dashboard."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from config import APP_NAME, APP_VERSION, LOGS_DIR, RUNTIME_DIR


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


def find_browser_app_executable() -> str:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    return next((path for path in candidates if os.path.isfile(path)), "")


def open_existing_dashboard(url: str) -> None:
    LOGGER.info("检测到已运行实例：%s", url)
    if not webbrowser.open(url, new=1):
        show_error(f"应用已在后台运行，请在浏览器中打开：\n{url}")


def launch_app_window(url: str) -> tuple[subprocess.Popen | None, Path | None]:
    if os.getenv("WECHAT_AI_SUMMARY_NO_BROWSER") == "1":
        LOGGER.info("测试模式：跳过浏览器窗口")
        return None, None
    browser_exe = find_browser_app_executable()
    if not browser_exe:
        webbrowser.open(url, new=1)
        return None, None
    profile_dir = Path(tempfile.mkdtemp(prefix="ui-", dir=RUNTIME_DIR))
    command = [
        browser_exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--window-size=1360,880",
        f"--app-title={APP_NAME}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    LOGGER.info("启动界面：%s", Path(browser_exe).name)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process, profile_dir


def remove_temporary_profile(profile_dir: Path | None, retries: int = 20) -> bool:
    if not profile_dir:
        return True
    for _ in range(max(1, retries)):
        shutil.rmtree(profile_dir, ignore_errors=True)
        if not profile_dir.exists():
            return True
        time.sleep(0.25)
    LOGGER.warning("临时浏览器目录未能立即清理，将由系统临时目录策略处理：%s", profile_dir)
    return False


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

    app_process = None
    profile_dir = None
    try:
        app_process, profile_dir = launch_app_window(url)
        while not state.should_exit:
            if app_process is not None and app_process.poll() is not None:
                LOGGER.info("界面窗口已关闭，应用同步退出")
                state.should_exit = True
                break
            time.sleep(0.5)
    finally:
        state.cleanup()
        server.shutdown()
        server.server_close()
        if app_process is not None and app_process.poll() is None:
            app_process.terminate()
            try:
                app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app_process.kill()
        remove_temporary_profile(profile_dir)
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
