"""OneBot / NapCat 消息表情回应（开始处理时的静默 ACK）。"""

from __future__ import annotations

from typing import Any

from ..config import log, maiconfig

REACT_EMOJI_CHECK = "9989"


def _is_official_qq(bot: Any, event: Any) -> bool:
    modules = {
        type(bot).__module__,
        type(getattr(bot, "adapter", None)).__module__,
        type(event).__module__,
    }
    return any(
        module.startswith("nonebot.adapters.qq") or ".adapters.qq" in module
        for module in modules
    )


async def react_processing(bot: Any, event: Any, *, emoji_id: str | None = None) -> bool:
    """对触发消息贴表情，表示已开始处理；失败时静默忽略。

    依赖适配器扩展 ``set_msg_emoji_like``（NapCat / LLOneBot 等）。
    官方 QQ 适配器或不支持该 API 时直接返回 False。
    """
    if _is_official_qq(bot, event):
        return False
    message_id = getattr(event, "message_id", None)
    if message_id is None or bot is None:
        return False
    eid = (
        emoji_id
        if emoji_id is not None
        else str(getattr(maiconfig, "maimaidx_processing_emoji_id", "424") or "424")
    )
    if not eid:
        return False
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=message_id,
            emoji_id=eid,
            set=True,
        )
        return True
    except Exception as exc:
        log.debug(f"[reaction] set_msg_emoji_like 失败: {type(exc).__name__}: {exc}")
        return False
