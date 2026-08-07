"""帮助/help 命令简洁回复回归测试。

用户发送 `帮助` 或 `help` 后，Bot 应回复：
    机器人帮助请前往
    https://wiki.awmc.team/guide/bot/intro

原先 `帮助maimaiDX` 回复一张大图，过于臃肿；新增独立的简洁文本入口。
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
source = (ROOT / "command" / "mai_base.py").read_text(encoding="utf-8")

# 1. 必须注册裸命令 `帮助`，且 `help` 作为别名
assert "on_command('帮助'" in source, "缺少 on_command('帮助') 注册"
# 别名 help 必须出现在该注册行的 aliases 中
help_reg_line_start = source.index("on_command('帮助'")
help_reg_line_end = source.index("\n", help_reg_line_start)
help_reg_line = source[help_reg_line_start:help_reg_line_end]
assert "aliases" in help_reg_line, "帮助命令缺少 aliases"
assert "'help'" in help_reg_line or '"help"' in help_reg_line, (
    "帮助命令别名未包含 help"
)

# 2. 必须回复简洁文本，不发送图片
#    精确定位 @short_help.handle() 处理器块（到下一个顶层 @ 装饰器）
handler_anchor = source.index("@short_help.handle()")
handler_idx = source.index("@", handler_anchor)
next_at = source.find("\n@", handler_idx + 1)
handler_block = source[handler_idx:next_at]

assert "机器人帮助请前往" in handler_block, "处理器未回复「机器人帮助请前往」"
assert "https://wiki.awmc.team/guide/bot/intro" in handler_block, (
    "处理器未包含帮助链接 https://wiki.awmc.team/guide/bot/intro"
)
assert "MessageSegment.image" not in handler_block, "帮助命令不应再发送图片"
assert "maimaidxhelp.png" not in handler_block, "帮助命令不应再引用 maimaidxhelp.png"

# 3. 不应使用 reply_message=True：官方 QQ 下会占用被动回复配额（5 分钟窗口、
#    单消息 5 次上限），帮助命令不需要引用效果。
assert "reply_message" not in handler_block, (
    "帮助命令不应使用 reply_message=True，避免占用官方 QQ 被动回复配额"
)

print("help short reply tests: ok")
