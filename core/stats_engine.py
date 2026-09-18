import hashlib
from collections import defaultdict
from datetime import datetime
from typing import List, Dict

from core.models import ChatMessage, MemberDailyStat, GroupDailySummary


class StatsEngine:
    """群聊消息聚合统计核心计算引擎"""

    # 常见微信群系统提示词（用于辅助过滤干扰统计的非发言内容）
    SYSTEM_PATTERNS = [
        "加入了群聊",
        "移出了群聊",
        "邀请了",
        "撤回了一条消息",
        "你撤回了一条消息",
        "拍了拍",
        "修改群名为",
        "扫码加入群聊",
        "开启了群聊邀请确认",
        "移出群聊",
        "解散了群聊",
        "打招呼吧",
    ]

    def __init__(self, filter_system_messages: bool = True):
        self.filter_system_messages = filter_system_messages

    def is_system_message(self, content: str) -> bool:
        """识别是否为群系统提示而非群员实际发言"""
        if not self.filter_system_messages:
            return False
        clean = content.strip()
        for pat in self.SYSTEM_PATTERNS:
            if pat in clean:
                return True
        return False

    def compute_daily_summary(
        self,
        messages: List[ChatMessage],
        group_name: str,
        date_str: str
    ) -> GroupDailySummary:
        """
        计算某一个群在一天的完整统计汇总
        :param messages: 待统计的消息列表（属于该群该天）
        :param group_name: 群名称
        :param date_str: 日期字符串 YYYY-MM-DD
        """
        # 1. 过滤不合规或系统消息
        valid_messages: List[ChatMessage] = []
        for m in messages:
            if self.is_system_message(m.content):
                continue
            valid_messages.append(m)

        total_messages = len(valid_messages)
        total_words = sum(m.word_count for m in valid_messages)

        # 2. 按群员身份分组聚合。昵称可能重复，“未知成员”也可能对应多人；
        # 有 sender_id 时必须用稳定身份聚合，避免把不同成员错误合并。
        member_dict: Dict[str, Dict] = defaultdict(lambda: {
            "nickname": "未知昵称",
            "member_key": "",
            "count": 0,
            "words": 0,
            "first_time": None,
            "last_time": None
        })

        # 24小时分布
        hourly_dist: Dict[int, int] = {h: 0 for h in range(24)}

        for m in valid_messages:
            nick = m.sender_nickname.strip() or "未知昵称"
            sender_id = str(m.sender_id).strip() if m.sender_id is not None else ""
            member_key = f"id:{sender_id}" if sender_id else f"nickname:{nick}"
            info = member_dict[member_key]
            info["nickname"] = nick
            info["member_key"] = member_key
            info["count"] += 1
            info["words"] += m.word_count
            time_str = m.time_str
            if info["first_time"] is None or time_str < info["first_time"]:
                info["first_time"] = time_str
            if info["last_time"] is None or time_str > info["last_time"]:
                info["last_time"] = time_str

            hourly_dist[m.hour] += 1

        # 3. 生成群员统计列表并排序
        member_stats: List[MemberDailyStat] = []
        for info in member_dict.values():
            count = info["count"]
            ratio = (count / total_messages) if total_messages > 0 else 0.0
            member_stats.append(MemberDailyStat(
                nickname=info["nickname"],
                member_key=hashlib.sha256(info["member_key"].encode("utf-8")).hexdigest(),
                message_count=count,
                total_words=info["words"],
                ratio=ratio,
                first_msg_time=info["first_time"],
                last_msg_time=info["last_time"]
            ))

        # 按发言条数降序排列，条数相同时按字数降序
        member_stats.sort(key=lambda x: (x.message_count, x.total_words), reverse=True)

        # 赋予名次排名
        for idx, stat in enumerate(member_stats, 1):
            stat.rank = idx

        # 4. 计算峰值时段
        peak_hour = None
        peak_hour_count = 0
        for h, cnt in hourly_dist.items():
            if cnt > peak_hour_count:
                peak_hour = h
                peak_hour_count = cnt

        return GroupDailySummary(
            group_name=group_name,
            date_str=date_str,
            total_messages=total_messages,
            total_members_spoke=len(member_stats),
            total_words=total_words,
            peak_hour=peak_hour,
            peak_hour_count=peak_hour_count,
            member_stats=member_stats,
            hourly_distribution=hourly_dist
        )
