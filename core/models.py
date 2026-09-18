from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional


@dataclass
class ChatMessage:
    """单个聊天消息数据模型"""
    timestamp: datetime
    group_name: str
    sender_nickname: str
    content: str
    msg_type: str = "text"  # text, image, emoji, voice, file, system, etc.
    sender_id: Optional[str] = None  # 微信号或wxid（若能获取到）
    id: Optional[int] = None

    @property
    def date_str(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d")

    @property
    def time_str(self) -> str:
        return self.timestamp.strftime("%H:%M:%S")

    @property
    def hour(self) -> int:
        return self.timestamp.hour

    @property
    def word_count(self) -> int:
        if self.msg_type != "text":
            return 0
        return len(self.content.strip())


@dataclass
class MemberDailyStat:
    """单个群员一天的聊天统计数据"""
    nickname: str
    member_key: str = ""
    message_count: int = 0
    total_words: int = 0
    ratio: float = 0.0  # 占当天总发言比例 (0.0 - 1.0)
    first_msg_time: Optional[str] = None
    last_msg_time: Optional[str] = None
    rank: int = 0

    @property
    def avg_words_per_msg(self) -> float:
        if self.message_count == 0:
            return 0.0
        return round(self.total_words / self.message_count, 1)


@dataclass
class GroupDailySummary:
    """某一个群某一天的整体统计汇总"""
    group_name: str
    date_str: str
    total_messages: int = 0
    total_members_spoke: int = 0
    total_words: int = 0
    peak_hour: Optional[int] = None  # 最活跃的小时（0-23）
    peak_hour_count: int = 0
    member_stats: List[MemberDailyStat] = field(default_factory=list)
    hourly_distribution: Dict[int, int] = field(default_factory=lambda: {h: 0 for h in range(24)})
