"""段位认定课题表查询指令。"""

from __future__ import annotations

from typing import Optional

from nonebot import on_regex
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.params import RegexMatched

from ..libraries.image import image_to_base64
from ..libraries.maimaidx_rank_course import (
    generate_rank_course_image,
    get_rank_course,
    rank_course_help,
)
from ..libraries.maimaidx_platform import billing_user_id, parse_at_target_id, resolve_score_qqid
from ..libraries.maimaidx_timing import finish_timed


rank_course_query = on_regex(r"^/?(?:段位表(?:\s+(.+))?|(.+?)段位表)\s*$")


def _at_qq(event: MessageEvent) -> Optional[int]:
    target = parse_at_target_id(event)
    if target is None:
        return None
    return resolve_score_qqid(event, target)


async def _generate_message(rank_name: str, qqid: Optional[int], username: Optional[str]):
    image = await generate_rank_course_image(
        rank_name,
        qqid=qqid,
        username=username,
    )
    return MessageSegment.image(image_to_base64(image))


@rank_course_query.handle()
async def _(event: MessageEvent, match=RegexMatched()):
    raw = (match.group(1) or match.group(2) or "").strip()
    if not raw:
        await rank_course_query.finish(rank_course_help(), reply_message=True)

    parts = raw.split(maxsplit=1)
    rank_name = parts[0]
    username = parts[1].strip() if len(parts) > 1 else None
    if get_rank_course(rank_name) is None:
        await rank_course_query.finish(
            f"无法识别段位「{rank_name}」\n\n{rank_course_help()}",
            reply_message=True,
        )

    qqid = _at_qq(event) or (None if username else resolve_score_qqid(event))
    await finish_timed(
        rank_course_query,
        _generate_message(rank_name, qqid, username),
        billing_qqid=billing_user_id(event),
        event=event,
    )
