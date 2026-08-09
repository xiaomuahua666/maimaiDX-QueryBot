"""Global announcement commands and command-execution gate."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from nonebot import on_command
from nonebot.adapters import Bot, Event
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import run_preprocessor
from nonebot.params import CommandArg
from nonebot.typing import T_State

from ..config import log
from ..libraries.maimaidx_announcement import (
    Announcement,
    announcement_db,
    format_announcement,
)
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY, is_plugin_admin
from ..libraries.maimaidx_platform import billing_user_id
from ..libraries.maimaidx_qrcode_util import message_may_contain_qrcode


announcement_publish = on_command(
    "发布公告", aliases={"创建公告"}, permission=PLUGIN_ADMIN_ONLY
)
announcement_edit = on_command("编辑公告", permission=PLUGIN_ADMIN_ONLY)
announcement_delete = on_command("删除公告", permission=PLUGIN_ADMIN_ONLY)
announcement_list = on_command("公告列表")
announcement_confirm = on_command(
    "确认阅读公告", aliases={"确认公告"}
)

for _matcher in (
    announcement_publish,
    announcement_edit,
    announcement_delete,
    announcement_list,
    announcement_confirm,
):
    setattr(_matcher, "_maimaidx_announcement_exempt", True)
    setattr(_matcher, "_maimaidx_busy_surcharge_exempt", True)

for _matcher in (announcement_list, announcement_confirm):
    setattr(_matcher, "_maimaidx_debt_exempt", True)


_pending_required: dict[str, tuple[int, int]] = {}
_blocked_events: dict[str, float] = {}
_BLOCK_EVENT_SECONDS = 15.0
_MAX_CONTENT_LENGTH = 2000


def _plain_arg(args) -> str:
    try:
        return str(args.extract_plain_text()).strip()
    except Exception:
        return str(args or "").strip()


def _user_key(event: Event) -> str:
    try:
        return str(billing_user_id(event))
    except Exception:
        return str(event.get_user_id())


def _mode_prefix(text: str) -> tuple[Optional[bool], str]:
    value = str(text).strip()
    for label, required in (("不必读", False), ("必读", True)):
        if value == label:
            return required, ""
        if value.startswith(label + " ") or value.startswith(label + "\n"):
            return required, value[len(label):].strip()
    return None, value


def _validate_content(content: str) -> Optional[str]:
    if not content.strip():
        return "公告内容不能为空。"
    if len(content) > _MAX_CONTENT_LENGTH:
        return f"公告内容不能超过 {_MAX_CONTENT_LENGTH} 字。"
    return None


def _matcher_module_name(matcher: Matcher) -> str:
    for obj in (matcher, type(matcher)):
        name = getattr(obj, "module_name", None)
        if name:
            return str(name)
    module = getattr(matcher, "module", None)
    if module is None:
        module = getattr(type(matcher), "module", None)
    if isinstance(module, str):
        return module
    name = getattr(module, "__name__", None) if module is not None else None
    return str(name) if name else ""


def _plugin_matcher(matcher: Matcher) -> bool:
    module = _matcher_module_name(matcher)
    plugin_name = str(getattr(matcher, "plugin_name", "") or "")
    raw_module = str(getattr(matcher, "module", "") or "")
    return "maimaidx" in f"{module} {plugin_name} {raw_module}".lower()


def _announcement_gate_exempt(matcher: Matcher) -> bool:
    matcher_type = type(matcher)
    return bool(
        getattr(matcher_type, "_maimaidx_announcement_exempt", False)
        or getattr(matcher_type, "_maimaidx_passive_recorder", False)
        or getattr(matcher_type, "_maimaidx_deferred_audit", False)
    )


def _matcher_is_instruction(matcher: Matcher) -> bool:
    """公告只拦截显式指令，不能影响普通聊天/被动消息监听。"""
    if getattr(matcher, "_maimaidx_announcement_exempt", False) or getattr(
        type(matcher), "_maimaidx_announcement_exempt", False
    ):
        return False
    rule = getattr(matcher, "rule", None)
    for checker in getattr(rule, "checkers", ()) or ():
        call = getattr(checker, "call", None)
        if type(call).__name__ in {"CommandRule", "RegexRule"}:
            return True
    return False


def _event_key(bot: Bot, event: Event) -> str:
    event_id = getattr(event, "message_id", None) or getattr(event, "id", None)
    if event_id is None:
        event_id = f"object-{id(event)}"
    return f"{getattr(bot, 'self_id', '')}:{event_id}"


def _trim_blocked_events(now: float) -> None:
    expired = [
        key
        for key, created_at in _blocked_events.items()
        if now - created_at > _BLOCK_EVENT_SECONDS
    ]
    for key in expired:
        _blocked_events.pop(key, None)


def _required_prompt(announcement: Announcement, *, show_id: bool) -> str:
    return (
        format_announcement(
            announcement, show_id=show_id, include_current=True
        )
        + "\n\n此公告为必读公告。请输入「确认阅读公告」完成确认，"
        "然后重新执行刚才的指令。"
    )


@run_preprocessor
async def _announcement_preprocessor(
    matcher: Matcher, bot: Bot, event: Event, state: T_State
):
    del state
    if (
        not _plugin_matcher(matcher)
        or _announcement_gate_exempt(matcher)
        or not _matcher_is_instruction(matcher)
    ):
        return

    # 敏感二维码必须先由业务处理器撤回；撤回后会显式调用同一公告门禁。
    try:
        if message_may_contain_qrcode(event.get_plaintext()):
            return
    except Exception:
        pass

    if not await enforce_current_announcement(bot, event):
        raise IgnoredException("required announcement awaiting confirmation")


async def enforce_current_announcement(bot: Bot, event: Event) -> bool:
    """展示当前公告；必读未确认时返回 ``False`` 阻止后续操作。"""

    user_key = _user_key(event)
    unseen = await asyncio.to_thread(announcement_db.unseen_current, user_key)
    if unseen is None:
        return True

    if unseen.required:
        now = time.monotonic()
        _trim_blocked_events(now)
        key = _event_key(bot, event)
        if key in _blocked_events:
            return False
        _blocked_events[key] = now
        _pending_required[user_key] = (unseen.id, unseen.revision)
        try:
            await bot.send(
                event,
                _required_prompt(
                    unseen, show_id=is_plugin_admin(event.get_user_id())
                ),
            )
        except Exception:
            _pending_required.pop(user_key, None)
            _blocked_events.pop(key, None)
            raise
        return False

    claimed = await asyncio.to_thread(
        announcement_db.claim_optional_current, user_key
    )
    if claimed is None:
        return True
    try:
        await bot.send(
            event,
            format_announcement(
                claimed,
                show_id=is_plugin_admin(event.get_user_id()),
                include_current=True,
            ),
        )
    except Exception as exc:
        await asyncio.to_thread(
            announcement_db.unmark_seen,
            user_key,
            claimed.id,
            claimed.revision,
        )
        log.warning(f"普通公告发送失败：{type(exc).__name__}: {exc}")
        return True
    await asyncio.sleep(1)
    return True


@announcement_publish.handle()
async def _(event: Event, args=CommandArg()):
    raw = _plain_arg(args)
    required, content = _mode_prefix(raw)
    required = bool(required) if required is not None else False
    error = _validate_content(content)
    if error:
        await announcement_publish.finish(
            error + "\n用法：发布公告 [必读|不必读] <内容>\n默认：不必读"
        )
    item = await asyncio.to_thread(
        announcement_db.create, content, required=required
    )
    await announcement_publish.finish(
        f"公告 #{item.id} 已发布并设为当前公告。\n"
        f"类型：{'必读' if item.required else '不必读'} · 版本 {item.revision}"
    )


@announcement_edit.handle()
async def _(args=CommandArg()):
    raw = _plain_arg(args)
    parts = raw.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await announcement_edit.finish(
            "用法：编辑公告 <ID> [必读|不必读] [新内容]"
        )
    announcement_id = int(parts[0])
    tail = parts[1].strip() if len(parts) > 1 else ""
    required, content = _mode_prefix(tail)
    new_content: Optional[str] = content or None
    if required is None and new_content is None:
        await announcement_edit.finish(
            "请提供新内容或必读状态。\n"
            "用法：编辑公告 <ID> [必读|不必读] [新内容]"
        )
    if new_content is not None:
        error = _validate_content(new_content)
        if error:
            await announcement_edit.finish(error)
    item = await asyncio.to_thread(
        announcement_db.update,
        announcement_id,
        content=new_content,
        required=required,
    )
    if item is None:
        await announcement_edit.finish(f"未找到公告 #{announcement_id}。")
    position = "当前公告" if item.is_current else "历史公告"
    await announcement_edit.finish(
        f"公告 #{item.id} 已更新（{position}）。\n"
        f"类型：{'必读' if item.required else '不必读'} · 版本 {item.revision}"
    )


@announcement_delete.handle()
async def _(args=CommandArg()):
    raw = _plain_arg(args)
    if not raw.isdigit():
        await announcement_delete.finish("用法：删除公告 <ID>")
    item = await asyncio.to_thread(announcement_db.delete, int(raw))
    if item is None:
        await announcement_delete.finish(f"未找到公告 #{raw}。")
    if item.is_current:
        _pending_required.clear()
    await announcement_delete.finish(
        f"公告 #{item.id} 已删除。"
        + ("\n当前已无生效公告，不会回退启用旧公告。" if item.is_current else "")
    )


@announcement_list.handle()
async def _(event: Event):
    items = await asyncio.to_thread(announcement_db.recent, 10)
    if not items:
        await announcement_list.finish("暂无公告。")
    admin = is_plugin_admin(event.get_user_id())
    blocks: list[str] = []
    for item in items:
        content = item.content
        if len(content) > 500:
            content = content[:500].rstrip() + "……"
        display = Announcement(
            id=item.id,
            content=content,
            required=item.required,
            revision=item.revision,
            is_current=item.is_current,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
        blocks.append(
            format_announcement(
                display, show_id=admin, include_current=True
            )
        )
    await announcement_list.finish("最近公告\n\n" + "\n\n".join(blocks))


async def _show_required_for_confirmation(
    event: Event, announcement: Announcement
) -> None:
    user_key = _user_key(event)
    _pending_required[user_key] = (announcement.id, announcement.revision)
    await announcement_confirm.finish(
        _required_prompt(
            announcement, show_id=is_plugin_admin(event.get_user_id())
        )
    )


@announcement_confirm.handle()
async def _(event: Event):
    user_key = _user_key(event)
    current = await asyncio.to_thread(announcement_db.current)
    pending = _pending_required.get(user_key)
    if current is None or not current.required:
        _pending_required.pop(user_key, None)
        await announcement_confirm.finish("当前没有需要确认的必读公告。")
    if pending != (current.id, current.revision):
        unseen = await asyncio.to_thread(announcement_db.unseen_current, user_key)
        if unseen is None:
            _pending_required.pop(user_key, None)
            await announcement_confirm.finish("当前必读公告已经确认过了。")
        await _show_required_for_confirmation(event, unseen)
        return
    marked = await asyncio.to_thread(
        announcement_db.mark_seen,
        user_key,
        current.id,
        current.revision,
    )
    _pending_required.pop(user_key, None)
    if not marked:
        await announcement_confirm.finish(
            "公告状态刚刚发生变化，请重新执行指令查看最新公告。"
        )
    await announcement_confirm.finish(
        "✅ 已确认阅读当前必读公告。\n"
        "该版本不会再次要求确认，请重新执行刚才的指令。"
    )
