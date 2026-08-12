#!/usr/bin/env python3
"""QQ context keyboards must send reachable commands and stay QQ-only."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('COMMAND_START', '["", "/"]')
os.environ.setdefault('MAIMAIDXPATH', str(ROOT / 'static'))
os.environ.setdefault('MAIMAIDX_STORAGE_FAIL_FAST', 'false')

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.command import (  # noqa: E402
    mai_account,
    mai_guess,
    mai_letter,
    mai_score,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_platform import (  # noqa: E402
    build_command_keyboard,
    build_command_keyboard_message,
    plugin_finish,
)
from nonebot.adapters.onebot.v11 import MessageSegment  # noqa: E402


qq_event = SimpleNamespace(
    group_openid='group-openid',
    get_user_id=lambda: 'member-openid',
)
onebot_event = SimpleNamespace(
    user_id=123456789,
    get_user_id=lambda: '123456789',
)


def command_map(buttons):
    keyboard = build_command_keyboard(
        buttons, event=qq_event, id_prefix='test-context'
    )
    data = keyboard.model_dump(exclude_none=True)
    rows = data['content']['rows']
    assert all(len(row['buttons']) <= 4 for row in rows)
    flattened = [button for row in rows for button in row['buttons']]
    for button in flattened:
        action = button['action']
        assert action['type'] == 2
        assert action['permission']['type'] == 2
        assert action['enter'] is True
        assert action['reply'] is False
    return {
        button['render_data']['label']: button['action']['data']
        for button in flattened
    }


b50 = command_map(mai_score._B50_SHORTCUTS)
assert b50['标准 B50'] == 'b50'
assert b50['AP50'] == 'ap50'
assert b50['FC50'] == 'fc50'
assert b50['寸50'] == '寸50'
assert b50['含金量'] == '含金量'
assert b50['含水量'] == '含水量'

content = command_map(mai_score._CONTENT_SHORTCUTS)
assert set(content) == {'含金量', '含水量', '我有多菜', '标准 B50'}

account = command_map(mai_account._ACCOUNT_SHORTCUTS)
assert account['MyMai'] == 'mymai'
assert account['游玩地图'] == 'mai地图'
assert account['发票 ×2'] == 'mai发票 2'
assert account['修改道具'] == 'mai改道具'

games = command_map(mai_guess._GUESS_SHORTCUTS)
assert games['再来猜歌'] == '猜歌'
assert games['再猜封面'] == '猜封面'
assert games['B50 找内鬼'] == '找内鬼'
assert games['极限二选一'] == '极限二选一'

letter = command_map(mai_letter._LETTER_SHORTCUTS)
assert letter['再来开字母'] == '开字母'

payload = build_command_keyboard_message(
    mai_guess._GUESS_SHORTCUTS,
    event=qq_event,
    title='🎮 再来一把',
)
assert [segment.type for segment in payload] == ['markdown', 'keyboard']
assert '再来一把' in payload[0].data['markdown'].content

# Other adapters retain the original message format and receive no keyboard.
assert build_command_keyboard(mai_score._B50_SHORTCUTS, event=onebot_event) is None


class FakeMatcher:
    def __init__(self):
        self.calls = []

    async def send(self, payload=None, *, reply_message=True):
        self.calls.append(('send', payload, reply_message))

    async def finish(self, payload=None, *, reply_message=True):
        self.calls.append(('finish', payload, reply_message))


# QQ image results are sent before a separate keyboard; OneBot remains one
# ordinary finish call with no keyboard follow-up.
qq_matcher = FakeMatcher()
asyncio.run(
    plugin_finish(
        qq_matcher,
        MessageSegment.image('base64://aW1hZ2U='),
        event=qq_event,
        qq_buttons=mai_score._B50_SHORTCUTS,
    )
)
assert [call[0] for call in qq_matcher.calls] == ['send', 'finish']
assert any(segment.type == 'file_image' for segment in qq_matcher.calls[0][1])
assert [segment.type for segment in qq_matcher.calls[1][1]] == [
    'markdown', 'keyboard'
]

onebot_matcher = FakeMatcher()
asyncio.run(
    plugin_finish(
        onebot_matcher,
        '普通结果',
        event=onebot_event,
        qq_buttons=mai_score._B50_SHORTCUTS,
    )
)
assert onebot_matcher.calls == [('finish', '普通结果', True)]

# Every emitted command has a registered matcher in its command family.
assert all(
    matcher is not None
    for matcher in (
        mai_score.best50,
        mai_score.apb50,
        mai_score.fcb50,
        mai_score.sun_b50,
        mai_score.gold_content,
        mai_score.water_content,
        mai_account.account_status,
        mai_account.account_region,
        mai_account.account_ticket,
        mai_account.account_item_upsert,
        mai_guess.guess_music_start,
        mai_guess.guess_music_pic,
        mai_guess.guess_impostor_start,
        mai_guess.guess_duel_start,
    )
)

print('qq context button tests: ok')
