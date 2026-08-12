#!/usr/bin/env python3
"""论坛绑定欢迎键盘必须发送可到达的 QQ 指令，并只奖励一次。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('MAIMAIDXPATH', str(ROOT / 'static'))
os.environ.setdefault('MAIMAIDX_STORAGE_FAIL_FAST', 'false')

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.command.mai_qq_bind import (  # noqa: E402
    _build_welcome_keyboard,
    _oauth_success_payload,
)
from nonebot_plugin_maimaidx.command.mai_base import mai_today, mai_what  # noqa: E402
from nonebot_plugin_maimaidx.command.mai_b50_analysis import (  # noqa: E402
    b50_analysis_cmd,
)
from nonebot_plugin_maimaidx.command.mai_break import awmc_checkin  # noqa: E402
from nonebot_plugin_maimaidx.command.mai_score import (  # noqa: E402
    today_gain_recommend,
    weekly_report,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_break import break_db  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_db import (  # noqa: E402
    UnifiedConnection,
)


keyboard = _build_welcome_keyboard().model_dump(exclude_none=True)
buttons = [button for row in keyboard['content']['rows'] for button in row['buttons']]
assert len(keyboard['content']['rows']) == 2
assert all(len(row['buttons']) <= 5 for row in keyboard['content']['rows'])

commands = {
    button['render_data']['label']: button['action']['data']
    for button in buttons
    if button['action']['type'] == 2
}
assert commands == {
    '签到': '签到',
    '今日舞萌': '今日舞萌',
    '锐评一下': '锐评一下',
    '吃分推荐': '吃分推荐',
    '我要上分': 'mai什么推分',
    '周报（需开启数据储存）': '周报',
}
# The target matchers are registered in QQ mode; type=2 + enter=True sends
# these command texts back as ordinary user messages through the same router.
assert all(
    matcher is not None
    for matcher in (
        awmc_checkin,
        mai_today,
        b50_analysis_cmd,
        today_gain_recommend,
        weekly_report,
        mai_what,
    )
)
for button in buttons:
    action = button['action']
    assert action['permission']['type'] == 2
    if action['type'] == 2:
        assert action['enter'] is True
        assert action['reply'] is False

assert re.fullmatch(r'.*mai.*什么(.+)?', commands['我要上分'])
help_button = next(button for button in buttons if button['id'] == 'welcome-help-link')
assert help_button['action'] == {
    'type': 0,
    'permission': {'type': 2},
    'data': 'https://wiki.awmc.team/guide/bot/intro',
}

# QQ 模式下必须同时产出 Markdown 和原生 keyboard 消息段。
event = SimpleNamespace(
    group_openid='group-openid',
    get_user_id=lambda: 'member-openid',
)
payload = _oauth_success_payload(
    {'username': '测试', 'email': '123@qq.com', 'legacy_qq': '123'},
    event,
    reward_awarded=True,
)
segments = list(payload)
assert [segment.type for segment in segments] == ['markdown', 'keyboard']
assert '3 BREAK' in segments[0].data['markdown'].content

# 用内存数据库验证永久幂等，不接触开发/生产余额库。
original_conn = break_db._conn
memory_conn = UnifiedConnection(backend='sqlite', db_path=':memory:')
try:
    # executescript 需要完整建表语句；直接从模块常量取得当前 schema。
    from nonebot_plugin_maimaidx.libraries.maimaidx_break import _CREATE_SQL

    memory_conn.executescript(_CREATE_SQL)
    break_db._conn = memory_conn
    qqid = 123456789
    before = break_db.get_balance(qqid)
    first = break_db.claim_once_reward(
        qqid, 'forum_bind_welcome', 3, reason='forum_bind_welcome'
    )
    second = break_db.claim_once_reward(
        qqid, 'forum_bind_welcome', 3, reason='forum_bind_welcome'
    )
    assert first.awarded is True
    assert second.awarded is False
    assert break_db.get_balance(qqid) == before + 3
finally:
    break_db._conn = original_conn

print('qq welcome keyboard tests: ok')
