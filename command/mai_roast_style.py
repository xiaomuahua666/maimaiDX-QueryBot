from __future__ import annotations

import asyncio

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..libraries.maimaidx_platform import plugin_finish, use_qq_mode
from ..libraries.maimaidx_roast_v2.policy import normalize_style
from ..libraries.maimaidx_roast_v2.style_store import clear_style, get_style, set_style


roast_style = on_command("锐评风格", aliases={"锐评样式"}, priority=4, block=True)


@roast_style.handle()
async def _handle(matcher: Matcher, event: MessageEvent, args: Message = CommandArg()):
    text = " ".join(args.extract_plain_text().strip().split())
    parts = text.split(maxsplit=1)
    action = parts[0] if parts else "查看"
    user_id = str(event.get_user_id())
    try:
        if action in {"查看", "当前"}:
            style = await asyncio.to_thread(get_style, user_id)
            message = "当前没有保存自定义风格。" if not style.raw else f"当前锐评风格：{style.raw}"
        elif action in {"设置", "保存"}:
            if len(parts) < 2 or not parts[1].strip():
                message = "请在“设置”后写风格描述，例如：锐评风格 设置 像可爱的猫娘女仆，偶尔加喵"
            else:
                style = await asyncio.to_thread(set_style, user_id, parts[1])
                message = f"已保存锐评风格：{style.raw}"
        elif action in {"重置", "清除", "删除"}:
            await asyncio.to_thread(clear_style, user_id)
            message = "已清除自定义风格，之后会使用自然口吻。"
        else:
            style = await asyncio.to_thread(set_style, user_id, text)
            message = f"已保存锐评风格：{style.raw}"
    except ValueError as exc:
        message = str(exc)
    await plugin_finish(matcher, message, event=event, mention_sender=use_qq_mode(event))
