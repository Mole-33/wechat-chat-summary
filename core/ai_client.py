"""Small OpenAI-compatible HTTP client used by all configured providers."""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List


class APIClientError(RuntimeError):
    pass


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise APIClientError("API 地址不能为空")
    suffix = suffix.lstrip("/")
    if base.endswith("/chat/completions") and suffix == "chat/completions":
        return base
    if base.endswith("/models") and suffix == "models":
        return base
    return f"{base}/{suffix}"


class CompatibleAIClient:
    def __init__(self, profile: Dict, proxy_url: str = "", proxy_username: str = "", proxy_password: str = ""):
        self.profile = dict(profile)
        self.api_key = str(profile.get("api_key") or "")
        self.timeout = int(profile.get("timeout_seconds") or 180)
        handlers = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        self.opener = urllib.request.build_opener(*handlers)
        self.proxy_auth = ""
        if proxy_username:
            token = f"{proxy_username}:{proxy_password}".encode("utf-8")
            self.proxy_auth = "Basic " + base64.b64encode(token).decode("ascii")

    def _request(self, method: str, url: str, payload=None):
        if not self.api_key:
            raise APIClientError("该平台尚未保存 API Key")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "WeChat-AI-Summary/0.1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.proxy_auth:
            headers["Proxy-Authorization"] = self.proxy_auth
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            try:
                parsed = json.loads(detail)
                detail = parsed.get("error", {}).get("message") or parsed.get("message") or detail
            except Exception:
                pass
            raise APIClientError(f"平台返回 HTTP {exc.code}：{detail}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise APIClientError(f"无法连接 AI 平台：{reason}") from None
        except json.JSONDecodeError:
            raise APIClientError("平台返回了无法解析的响应") from None

    def list_models(self) -> List[str]:
        url = _endpoint(str(self.profile.get("base_url") or ""), "models")
        payload = self._request("GET", url)
        data = payload.get("data", []) if isinstance(payload, dict) else []
        models = sorted({str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")})
        return models

    def chat(self, system_prompt: str, user_prompt: str) -> Dict:
        model = str(self.profile.get("model") or "").strip()
        if not model:
            raise APIClientError("请先选择或填写模型名称")
        url = _endpoint(str(self.profile.get("base_url") or ""), "chat/completions")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        if self.profile.get("kind") == "openai":
            body["store"] = False
        payload = self._request("POST", url, body)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise APIClientError("平台响应中缺少模型输出文本") from None
        return {"content": content or "", "usage": payload.get("usage", {})}

