from nonebot import on_keyword
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import MessageEvent

from ..libraries.maimaidx_platform import (
    build_mention_message,
    ensure_sender_mention,
    platform_user_id,
)

# 你画我猜小游戏链接
DRAW_GUESS_LINK = "https://v.wmc.pub/draw-guess"

# ---- 被动触发：消息任意位置出现「你画我猜」即触发 ----
# 完全模仿「红门」门攻略的触发机制：
# priority=99 让它在大多数业务指令之后响应；
# block=False 避免吞掉其他可能也想处理该消息的匹配器。
# 命中后 @ 触发者（即说「你画我猜」的对方）并附上小游戏链接。
draw_guess_keyword = on_keyword({"你画我猜"}, priority=99, block=False)


@draw_guess_keyword.handle()
async def _(event: MessageEvent):
    uid = platform_user_id(event)
    text = f" 来玩你画我猜小游戏 {DRAW_GUESS_LINK}"
    msg = build_mention_message(uid, text, event=event)
    msg = ensure_sender_mention(msg, event)
    await draw_guess_keyword.finish(msg)
