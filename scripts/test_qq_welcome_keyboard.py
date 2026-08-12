#!/usr/bin/env python3
"""论坛绑定欢迎键盘必须发送可到达的 QQ 指令，并只奖励一次。"""

from __future__ import annotations

import os
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
from nonebot_plugin_maimaidx.libraries.maimaidx_account_db import (  # noqa: E402
    account_db,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_lxns_db import (  # noqa: E402
    lxns_db,
)
from nonebot_plugin_maimaidx.command.mai_account import (  # noqa: E402
    account_bind,
    fish_bind,
    upload_all,
)
from nonebot_plugin_maimaidx.command.mai_break import awmc_checkin  # noqa: E402
from nonebot_plugin_maimaidx.command.mai_lxns import lxbind  # noqa: E402
from nonebot_plugin_maimaidx.command.mai_playcount import (  # noqa: E402
    my_pc,
    pc50,
    update_pc,
)
from nonebot_plugin_maimaidx.command.mai_score import (  # noqa: E402
    best50,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_break import break_db  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_db import (  # noqa: E402
    UnifiedConnection,
)


keyboard = _build_welcome_keyboard().model_dump(exclude_none=True)
buttons = [button for row in keyboard['content']['rows'] for button in row['buttons']]
assert len(keyboard['content']['rows']) <= 5
assert all(len(row['buttons']) <= 3 for row in keyboard['content']['rows'])

commands = {
    button['render_data']['label']: button['action']['data']
    for button in buttons
    if button['action']['type'] == 2
}
assert commands == {
    '绑定舞萌': 'mai绑定',
    '绑定水鱼': 'mai绑定水鱼',
    '绑定落雪': 'lxbind',
    '标准 B50': 'b50',
    'PC50': 'pc50',
    '我的 PC': '我的pc数',
    '更新 PC': '更新pc数',
    'MyMai': 'mymai',
    '签到': '签到',
}

# Once all three account layers are bound, onboarding buttons disappear and
# the same slots become the actual upload/B50/PC next steps.
state_event = SimpleNamespace(
    user_id=246813579,
    get_user_id=lambda: '246813579',
)
original_account_get = account_db.get
original_lxns_get = lxns_db.get_user
try:
    account_db.get = lambda _key: SimpleNamespace(
        qrcode='SGWCMAID=bound', fish_token='fish', lxns_token=''
    )
    lxns_db.get_user = lambda _qqid: {'access_token': 'lxns'}
    ready_keyboard = _build_welcome_keyboard(state_event).model_dump(
        exclude_none=True
    )
finally:
    account_db.get = original_account_get
    lxns_db.get_user = original_lxns_get
ready_commands = {
    button['render_data']['label']: button['action']['data']
    for row in ready_keyboard['content']['rows']
    for button in row['buttons']
    if button['action']['type'] == 2
}
assert not {'绑定舞萌', '绑定水鱼', '绑定落雪'} & set(ready_commands)
assert ready_commands['自动上传 B50'] == 'maiua'
assert ready_commands['标准 B50'] == 'b50'
assert ready_commands['PC50'] == 'pc50'
# The target matchers are registered in QQ mode; type=2 + enter=True sends
# these command texts back as ordinary user messages through the same router.
assert all(
    matcher is not None
    for matcher in (
        awmc_checkin,
        account_bind,
        fish_bind,
        lxbind,
        upload_all,
        best50,
        pc50,
        my_pc,
        update_pc,
    )
)
for button in buttons:
    action = button['action']
    assert action['permission']['type'] == 2
    if action['type'] == 2:
        assert action['enter'] is True
        assert action['reply'] is False

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
assert '论坛绑定成功' in segments[0].data['markdown'].content
assert 'OAuth' not in segments[0].data['markdown'].content

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
