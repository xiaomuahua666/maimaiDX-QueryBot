"""Official QQ compatibility mode keeps mentions but removes Markdown/keyboards."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault('COMMAND_START', '["", "/"]')
os.environ.setdefault('MAIMAIDXPATH', str(ROOT / 'static'))
os.environ.setdefault('MAIMAIDX_STORAGE_FAIL_FAST', 'false')

import nonebot  # noqa: E402

nonebot.init()

from nonebot_plugin_maimaidx.libraries.maimaidx_platform import (  # noqa: E402
    _markdown_to_plain_text,
    build_command_keyboard,
    build_markdown_message,
    ensure_sender_mention,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_bind import qq_bind_db  # noqa: E402


class FakeQQEvent:
    group_openid = 'plain-mode-group'
    author = {'member_openid': 'plain-mode-openid'}

    @staticmethod
    def get_user_id() -> str:
        return 'plain-mode-openid'


assert _markdown_to_plain_text(
    '## 授权\n[打开授权页面](https://example.test/oauth)\n**提示**'
) == '授权\n打开授权页面: https://example.test/oauth\n提示'

original = qq_bind_db.is_plain_text_mode
qq_bind_db.is_plain_text_mode = lambda platform_id: platform_id == 'plain-mode-openid'
try:
    event = FakeQQEvent()
    message = build_markdown_message(
        '## 授权\n[打开授权页面](https://example.test/oauth)', event=event
    )
    segments = list(message)
    assert [segment.type for segment in segments] == ['text']
    assert 'https://example.test/oauth' in str(segments[0])
    assert build_command_keyboard([('B50', 'b50')], event=event) is None

    mentioned = ensure_sender_mention('普通文本', event)
    mentioned_types = [segment.type for segment in mentioned]
    assert 'markdown' not in mentioned_types
    assert 'keyboard' not in mentioned_types
    assert any(item in mentioned_types for item in ('mention_user', 'at'))
finally:
    qq_bind_db.is_plain_text_mode = original

qq_bind_source = (ROOT / 'libraries' / 'maimaidx_qq_bind.py').read_text(encoding='utf-8')
qq_command_source = (ROOT / 'command' / 'mai_qq_bind.py').read_text(encoding='utf-8')
assert 'CREATE TABLE IF NOT EXISTS qq_user_preferences' in qq_bind_source
assert 'def set_plain_text_mode' in qq_bind_source
assert "on_command('兼容模式'" in qq_command_source
assert "'标准模式', aliases={'Markdown模式'" in qq_command_source

print('qq plain mode tests: ok')
