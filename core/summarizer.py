"""Token-aware map/reduce summarization with a fixed Chinese result schema."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List

from core.ai_client import CompatibleAIClient
from core.models import ChatMessage


CHUNK_TOKEN_LIMIT = 12_000


SYSTEM_PROMPT = """你是严谨的微信群聊分析助手。只根据提供的消息总结，不得补充不存在的事实。
所有关键结论、待办事项和未解决问题必须给出发送人、时间和不超过40字的原文依据。
如果无法确认负责人、截止时间或结论，明确写“未明确”，不要猜测。
只输出合法 JSON，不要使用 Markdown 代码围栏。"""


SCHEMA_INSTRUCTION = """返回以下固定结构：
{
  "core_summary": "核心摘要",
  "topics": [{"title":"话题","summary":"概述","evidence":[{"sender":"发送人","time":"YYYY-MM-DD HH:MM:SS","quote":"原文"}]}],
  "decisions": [{"text":"关键结论","evidence":[{"sender":"发送人","time":"时间","quote":"原文"}]}],
  "action_items": [{"task":"待办","owner":"负责人或未明确","deadline":"截止时间或未明确","evidence":[{"sender":"发送人","time":"时间","quote":"原文"}]}],
  "links_files": [{"type":"链接或文件","value":"内容","sender":"发送人","time":"时间"}],
  "open_questions": [{"question":"未解决问题","evidence":[{"sender":"发送人","time":"时间","quote":"原文"}]}],
  "active_members": [{"name":"成员","contribution":"主要贡献"}]
}"""


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    other = max(0, len(text) - chinese)
    return max(1, int(chinese * 1.15 + other / 3.8))


def format_message(message: ChatMessage) -> str:
    clean = message.content.replace("\x00", "").strip()
    return f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] {message.sender_nickname}: {clean}"


def chunk_messages(messages: Iterable[ChatMessage], limit: int = CHUNK_TOKEN_LIMIT) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for message in messages:
        line = format_message(message)
        cost = estimate_tokens(line) + 2
        if current and current_tokens + cost > limit:
            chunks.append("\n".join(current))
            current, current_tokens = [], 0
        current.append(line)
        current_tokens += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def _parse_json(content: str) -> Dict:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            text = match.group(0)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"core_summary": content.strip()}
    defaults = {
        "core_summary": "",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "links_files": [],
        "open_questions": [],
        "active_members": [],
    }
    defaults.update(parsed if isinstance(parsed, dict) else {})
    for key in defaults:
        if key != "core_summary" and not isinstance(defaults[key], list):
            defaults[key] = []
    return defaults


def summarize_messages(
    client: CompatibleAIClient,
    group_name: str,
    start: datetime,
    end: datetime,
    messages: List[ChatMessage],
    progress=None,
) -> Dict:
    if not messages:
        raise ValueError("所选时间范围内没有文字消息")
    chunks = chunk_messages(messages)
    partials = []
    total_calls = len(chunks) + (1 if len(chunks) > 1 else 0)
    call_index = 0
    for index, chunk in enumerate(chunks, 1):
        call_index += 1
        if progress:
            progress(call_index - 1, total_calls, f"正在调用 AI 总结第 {index}/{len(chunks)} 段")
        prompt = (
            f"群聊：{group_name}\n时间范围：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}\n"
            f"这是第 {index}/{len(chunks)} 段消息。\n{SCHEMA_INSTRUCTION}\n\n消息：\n{chunk}"
        )
        partials.append(_parse_json(client.chat(SYSTEM_PROMPT, prompt)["content"]))
        if progress:
            progress(call_index, total_calls, f"已完成第 {index}/{len(chunks)} 段")
    if len(partials) == 1:
        result = partials[0]
    else:
        call_index += 1
        if progress:
            progress(call_index - 1, total_calls, "正在调用 AI 合并分段总结")
        merge_prompt = (
            f"请将以下 {len(partials)} 份分段结果合并为一份去重后的群聊总结。"
            "不得新增分段结果中不存在的事实，保留原文依据。\n"
            f"群聊：{group_name}\n时间范围：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}\n"
            f"{SCHEMA_INSTRUCTION}\n\n分段结果：\n{json.dumps(partials, ensure_ascii=False)}"
        )
        result = _parse_json(client.chat(SYSTEM_PROMPT, merge_prompt)["content"])
        if progress:
            progress(call_index, total_calls, "分段总结合并完成")
    result["group_name"] = group_name
    result["start"] = start.isoformat(sep=" ", timespec="seconds")
    result["end"] = end.isoformat(sep=" ", timespec="seconds")
    result["message_count"] = len(messages)
    return result


def summary_to_markdown(summary: Dict) -> str:
    def evidence_text(items):
        parts = []
        for item in items or []:
            parts.append(f"{item.get('sender','未知')} · {item.get('time','')}：“{item.get('quote','')}”")
        return "；".join(parts)

    lines = [
        f"# {summary.get('group_name', '')} 群聊总结",
        "",
        f"> 时间：{summary.get('start', '')} 至 {summary.get('end', '')} · {summary.get('message_count', 0)} 条文字消息",
        "",
        "## 核心摘要",
        "",
        summary.get("core_summary") or "暂无",
    ]
    sections = [
        ("主要话题", "topics", lambda x: f"**{x.get('title','话题')}**：{x.get('summary','')}"),
        ("关键结论", "decisions", lambda x: x.get("text", "")),
        ("待办事项", "action_items", lambda x: f"{x.get('task','')}（负责人：{x.get('owner','未明确')}；截止：{x.get('deadline','未明确')}）"),
        ("重要链接/文件", "links_files", lambda x: f"{x.get('type','内容')}：{x.get('value','')}（{x.get('sender','')} {x.get('time','')}）"),
        ("未解决问题", "open_questions", lambda x: x.get("question", "")),
        ("活跃成员", "active_members", lambda x: f"{x.get('name','')}：{x.get('contribution','')}"),
    ]
    for title, key, formatter in sections:
        lines.extend(["", f"## {title}", ""])
        items = summary.get(key) or []
        if not items:
            lines.append("- 暂无")
            continue
        for item in items:
            line = formatter(item)
            evidence = evidence_text(item.get("evidence", [])) if isinstance(item, dict) else ""
            lines.append(f"- {line}" + (f"\n  - 依据：{evidence}" if evidence else ""))
    return "\n".join(lines).strip() + "\n"
