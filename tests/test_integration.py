"""Opt-in real-data test restricted to the explicitly authorized scope."""

import os
from datetime import date, datetime, time

import pytest

from listeners.wechat4_reader import WeChat4Reader


@pytest.mark.skipif(os.getenv("WECHAT_REAL_TEST") != "1", reason="真实微信测试需显式启用")
def test_authorized_group_range_can_be_read_without_logging_content():
    reader = WeChat4Reader()
    accounts = reader.accounts()
    assert accounts
    try:
        reader.connect(accounts[0]["account"])
        group = next(item for item in reader.groups() if item["name"] == "充电数据网站反馈1群")
        today = date.today()
        messages = reader.read_range(
            group["id"], group["name"],
            datetime.combine(today, time(0, 0)),
            datetime.combine(today, time(13, 0)),
        )
        assert messages, "授权时段内应至少读取到一条文字消息，不能把空列表误判为成功"
        assert all(datetime.combine(today, time(0, 0)) <= item.timestamp <= datetime.combine(today, time(13, 0)) for item in messages)
        messages.clear()
    finally:
        reader.close()
