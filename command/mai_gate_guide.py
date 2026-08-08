from nonebot import on_command, on_keyword
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.params import CommandArg

from ..libraries.maimaidx_platform import (
    build_mention_message,
    ensure_sender_mention,
    platform_user_id,
)

# 门名 -> 攻略子站（仅列出已验证存在独立子站的门）。
# 棱镜塔 / 表门 / 希望之门 / 里门 暂无独立攻略页，不在此列。
GATE_LINKS: dict[str, str] = {
    "蓝门": "https://blue.awmc.team",
    "青门": "https://blue.awmc.team",  # 蓝门在游戏内称"青门"，作为别名
    "白门": "https://white.awmc.team",
    "紫门": "https://purple.awmc.team",
    "黑门": "https://black.awmc.team",
    "黄门": "https://yellow.awmc.team",
    "红门": "https://red.awmc.team",
}

# 展示顺序（去重"青门"别名，只展示主名）
_GATE_DISPLAY_ORDER = ["蓝门", "白门", "紫门", "黑门", "黄门", "红门"]


def _gate_link_message(gate_name: str, uid: str, event: MessageEvent) -> Message:
    """构造 @用户 + 门攻略链接 的消息。"""
    url = GATE_LINKS[gate_name]
    text = f" {gate_name}攻略 {url}"
    msg = build_mention_message(uid, text, event=event)
    return ensure_sender_mention(msg, event)


# ---- 被动触发：消息任意位置出现门名即触发 ----
# priority=99 让它在大多数业务指令之后响应；
# block=False 避免吞掉其他可能也想处理该消息的匹配器。
# "哪些门" 作为列出全部门链接的触发关键词一并收录。
gate_keyword = on_keyword(set(GATE_LINKS.keys()) | {"哪些门"}, priority=99, block=False)


@gate_keyword.handle()
async def _(event: MessageEvent):
    uid = platform_user_id(event)
    text = event.get_message().extract_plain_text()
    # "哪些门"：@ 触发者并列出全部门攻略链接
    if "哪些门" in text:
        lines = ["🚪 门攻略列表"]
        for name in _GATE_DISPLAY_ORDER:
            lines.append(f"{name}：{GATE_LINKS[name]} \u200b")
        body = "\n".join(lines)
        msg = build_mention_message(uid, f"\n{body}", event=event)
        msg = ensure_sender_mention(msg, event)
        await gate_keyword.finish(msg)
        return
    # 取消息中第一个命中的门名
    for name in GATE_LINKS:
        if name in text:
            msg = _gate_link_message(name, uid, event)
            await gate_keyword.finish(msg)
            return


# ---- 主动查询：门攻略 [门名] ----
gate_command = on_command("门攻略", aliases={"门攻略列表"}, priority=50, block=True)


@gate_command.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    arg = args.extract_plain_text().strip()
    if arg:
        # 模糊匹配：支持"门攻略 红"和"门攻略 红门"
        matched = None
        for name in GATE_LINKS:
            if name.startswith(arg) or arg in name:
                matched = name
                break
        if matched:
            uid = platform_user_id(event)
            msg = _gate_link_message(matched, uid, event)
            await gate_command.finish(msg)
        else:
            await gate_command.finish(
                f"未找到「{arg}」对应的门攻略。\n"
                f"支持的门：{' / '.join(_GATE_DISPLAY_ORDER)}",
                reply_message=True,
            )
    else:
        # 无参数：列出所有门攻略链接
        lines = ["🚪 门攻略列表"]
        for name in _GATE_DISPLAY_ORDER:
            lines.append(f"{name}：{GATE_LINKS[name]} \u200b")
        await gate_command.finish("\n".join(lines), reply_message=True)
