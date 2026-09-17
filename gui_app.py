import sys
import os
import time
import socket
import threading
import subprocess
import webbrowser
from pathlib import Path

# 确保在 Windows 控制台下正确处理 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from http.server import ThreadingHTTPServer
from gui.server import AppHTTPRequestHandler, state
from config import APP_NAME


def find_free_port(start_port: int = 18989) -> int:
    """寻找本地空闲可用端口"""
    for p in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start_port


def find_browser_app_executable() -> str:
    """寻找系统内置的 Edge 或 Chrome 浏览器可执行路径以支持独立 App 窗口模式"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def main():
    print(f"正在启动 {APP_NAME}...")

    port = find_free_port(18989)
    url = f"http://127.0.0.1:{port}"

    # 1. 在本地绑定并启动 HTTP 服务
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), AppHTTPRequestHandler)
    except Exception as e:
        print(f"[!] 绑定端口 {port} 失败: {e}")
        return

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[OK] 本地可视化引擎已就绪: {url}")

    # 2. 独立 Edge/Chrome App 窗口模式启动
    browser_exe = find_browser_app_executable()
    app_proc = None

    if browser_exe:
        try:
            # 使用独立用户数据目录，避免附加到现有后台 Edge 进程而导致立即退出的问题
            profile_dir = Path(os.path.expandvars(r"%LOCALAPPDATA%\WeChatAISummary\edge_app_profile"))
            profile_dir.mkdir(parents=True, exist_ok=True)

            print(f"[+] 正在以原生独立桌面视窗模式打开界面 (渲染核心: {Path(browser_exe).name})...")
            cmd = [
                browser_exe,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--window-size=1360,880",
                f"--app-title={APP_NAME}",
                "--no-first-run",
                "--no-default-browser-check"
            ]
            app_proc = subprocess.Popen(cmd)
        except Exception as e:
            print(f"[!] 独立视窗启动异常: {e}，正在切换为系统默认浏览器...")
            browser_exe = ""

    if not browser_exe or not app_proc:
        print("[+] 正在打开系统默认浏览器访问看板...")
        webbrowser.open(url)

    print(f"仪表盘地址: {url}")
    print("应用运行期间可执行实时统计与定时总结；退出后不会在后台运行。")

    # 3. 稳健常驻主循环：绝不因视窗休眠、后台节流或最小化而意外关闭
    try:
        while not state.should_exit:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[+] 接收到用户退出信号 (Ctrl+C)，正在安全停止服务并保存数据...")
    finally:
        state.cleanup()
        server.shutdown()

    print("[+] 微信群统计 GUI 桌面服务已退出。感谢使用！")


if __name__ == "__main__":
    main()
