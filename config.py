"""Application paths and non-secret defaults."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "微信群聊 AI 总结助手"
APP_VERSION = "0.1.1"
GITHUB_REPOSITORY = "Mole-33/-"

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

if os.getenv("WECHAT_AI_SUMMARY_HOME"):
    USER_DATA_DIR = Path(os.environ["WECHAT_AI_SUMMARY_HOME"]).expanduser().resolve()
elif getattr(sys, "frozen", False):
    local_app_data = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(BASE_DIR)
    USER_DATA_DIR = Path(local_app_data) / "WeChatAISummary"
else:
    USER_DATA_DIR = BASE_DIR

DATA_DIR = USER_DATA_DIR / "data"
REPORTS_DIR = USER_DATA_DIR / "reports"
EXPORTS_DIR = USER_DATA_DIR / "exports"
LOGS_DIR = USER_DATA_DIR / "logs"
RUNTIME_DIR = Path(os.getenv("TEMP", str(BASE_DIR))) / "wechat-ai-summary-runtime"
SETTINGS_PATH = DATA_DIR / "settings.json"
SECRETS_PATH = DATA_DIR / "secrets.bin"
DB_PATH = DATA_DIR / "wechat_summary_stats.db"

for directory in (DATA_DIR, REPORTS_DIR, EXPORTS_DIR, LOGS_DIR, RUNTIME_DIR):
    directory.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "poll_interval_seconds": 2.0,
    "filter_system_messages": True,
    "daily_summary_time": "23:30",
    "selected_account": "",
    "selected_groups": [],
    "active_provider_id": "",
    "proxy_url": "",
    "schedule_enabled": False,
    "first_backfill": {},
}

PROVIDER_DEFAULTS = {
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1"},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com"},
    "qwen": {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "zhipu": {"name": "智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "doubao": {"name": "豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    "siliconflow": {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1"},
    "custom": {"name": "自定义兼容接口", "base_url": ""},
}
