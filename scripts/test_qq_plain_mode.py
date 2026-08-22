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
    _format_plain_qq_markdown,
    _markdownize_web_links,
    _markdown_to_plain_text,
    _qq_text_message_as_markdown,
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


class RichLinkQQEvent(FakeQQEvent):
    @staticmethod
    def get_user_id() -> str:
        return 'rich-link-openid'


assert _markdown_to_plain_text(
    '## 授权\n[打开授权页面](https://example.test/oauth)\n**提示**'
) == '授权\n打开授权页面: https://example.test/oauth\n提示'
assert _format_plain_qq_markdown(
    '查询完成\n成绩：100.5000%\n· 已写入缓存'
) == '## 查询完成\n- **成绩：** 100.5000%\n- 已写入缓存'
assert _format_plain_qq_markdown(
    '✅ AWMC 签到成功！\n━━━━━━━━━━━━━━\n📅 连续签到：8 天\n💰 获得：5 BREAK'
) == (
    '## ✅ AWMC 签到成功！\n\n'
    '- **📅 连续签到：** 8 天\n'
    '- **💰 获得：** 5 BREAK'
)
assert _format_plain_qq_markdown('## 已格式化\n- 保持原样') == '## 已格式化\n- 保持原样'

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

link_url = 'https://wiki.awmc.team/guide/bot/intro'
direct_link = _qq_text_message_as_markdown(f'帮助文档\n· 使用说明：{link_url}')
assert [segment.type for segment in direct_link] == ['markdown']
direct_content = direct_link[0].data['markdown'].content
assert f'[{link_url}]({link_url})' in direct_content
assert '<qqbot-at-user' not in direct_content

rich_link_event = RichLinkQQEvent()
linked_markdown = build_markdown_message(
    f'## 帮助文档\n- [打开使用说明]({link_url})',
    event=rich_link_event,
)
assert [segment.type for segment in linked_markdown] == ['markdown']
assert f'[打开使用说明]({link_url})' in linked_markdown[0].data['markdown'].content

mentioned_link = ensure_sender_mention(
    f'帮助文档\n· 使用说明：{link_url}', rich_link_event,
)
mentioned_link_types = [segment.type for segment in mentioned_link]
assert mentioned_link_types == ['markdown']
mentioned_content = mentioned_link[0].data['markdown'].content
assert mentioned_content.startswith(
    '<qqbot-at-user id="rich-link-openid" />\n'
)
assert f'[{link_url}]({link_url})' in mentioned_content

assert _markdownize_web_links(
    f'🎮 游戏地址 {link_url}'
) == f'🎮 游戏地址 [{link_url}]({link_url})'
assert _markdownize_web_links(f'[说明]({link_url})') == f'[说明]({link_url})'
announcement_body = (
    '【必读公告 · 当前】\n'
    '🎁 宣传你画我猜 瓜分现金奖励\n'
    f'🎮 游戏地址 {link_url}\n'
    '确认词：来玩你画我猜'
)
announcement_reply = ensure_sender_mention(
    announcement_body, rich_link_event,
)
assert [segment.type for segment in announcement_reply] == ['markdown']
announcement_content = announcement_reply[0].data['markdown'].content
assert announcement_content.startswith(
    '<qqbot-at-user id="rich-link-openid" />\n'
)
assert f'[{link_url}]({link_url})' in announcement_content
assert '**确认词：** 来玩你画我猜' in announcement_content

qq_bind_source = (ROOT / 'libraries' / 'maimaidx_qq_bind.py').read_text(encoding='utf-8')
qq_command_source = (ROOT / 'command' / 'mai_qq_bind.py').read_text(encoding='utf-8')
assert 'CREATE TABLE IF NOT EXISTS qq_user_preferences' in qq_bind_source
assert 'def set_plain_text_mode' in qq_bind_source
assert "on_command('兼容模式'" in qq_command_source
assert "'标准模式', aliases={'Markdown模式'" in qq_command_source

print('qq plain mode tests: ok')
