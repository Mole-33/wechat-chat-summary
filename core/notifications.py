"""Best-effort Windows desktop notifications."""

from __future__ import annotations


def notify(title: str, message: str) -> None:
    try:
        from winotify import Notification
        Notification(app_id="微信群聊 AI 总结助手", title=title, msg=message).show()
    except Exception:
        # Notifications are helpful but must never break a summary job.
        return

