"""QQ 官方机器人自定义菜单 / 指令面板管理指令（仅插件管理员）。

这些指令调用 QQ 服务端 OpenAPI 配置单聊底部菜单和指令面板，
需要使用官方 QQ 适配器（``nonebot-adapter-qq``）的 Bot 凭据。
"""

from __future__ import annotations

from typing import Optional

from nonebot import on_command
from nonebot.adapters import Bot, Event
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from ..config import log
from ..libraries import maimaidx_qq_menu as qm
from ..libraries.maimaidx_bot_admin import PLUGIN_ADMIN_ONLY
from ..libraries.maimaidx_platform import _bot_registry_values, is_qq_bot

# --- matchers -------------------------------------------------------------

set_menu_cmd = on_command(
    "设置QQ菜单",
    aliases={"推送QQ菜单", "同步QQ菜单"},
    permission=PLUGIN_ADMIN_ONLY,
)
get_menu_cmd = on_command(
    "QQ菜单",
    aliases={"查询QQ菜单", "查看QQ菜单"},
    permission=PLUGIN_ADMIN_ONLY,
)
set_panel_cmd = on_command(
    "设置QQ面板",
    aliases={"推送QQ面板", "同步QQ面板"},
    permission=PLUGIN_ADMIN_ONLY,
)
list_panel_cmd = on_command(
    "QQ面板列表",
    aliases={"QQ面板", "查询QQ面板"},
    permission=PLUGIN_ADMIN_ONLY,
)
panel_detail_cmd = on_command(
    "QQ面板详情",
    aliases={"查看QQ面板"},
    permission=PLUGIN_ADMIN_ONLY,
)
delete_panel_cmd = on_command(
    "删除QQ面板",
    aliases={"移除QQ面板"},
    permission=PLUGIN_ADMIN_ONLY,
)

for _m in (
    set_menu_cmd,
    get_menu_cmd,
    set_panel_cmd,
    list_panel_cmd,
    panel_detail_cmd,
    delete_panel_cmd,
):
    setattr(_m, "_maimaidx_busy_surcharge_exempt", True)
    setattr(_m, "_maimaidx_debt_exempt", True)
    setattr(_m, "_maimaidx_announcement_exempt", True)


# --- helpers --------------------------------------------------------------


def _resolve_qq_bot(bot: Bot) -> Optional[Bot]:
    """优先使用事件自带的 QQ Bot；否则从已连接 Bot 中找一个官方 QQ Bot。"""
    if is_qq_bot(bot):
        return bot
    for candidate in _bot_registry_values():
        if is_qq_bot(candidate):
            return candidate
    return None


def _plain_arg(args) -> str:
    try:
        return str(args.extract_plain_text()).strip()
    except Exception:
        return str(args or "").strip()


def _format_api_error(exc: Exception) -> str:
    """把 QQ 适配器异常格式化成对管理员友好的文本。"""
    info = repr(exc)
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if status or code or message:
        parts = [f"HTTP {status}" if status else ""]
        if code is not None:
            parts.append(f"code={code}")
        if message:
            parts.append(f"message={message}")
        info = " ".join(p for p in parts if p)
    return f"QQ 接口调用失败：{info}"


# --- handlers -------------------------------------------------------------


@set_menu_cmd.handle()
async def _(matcher: Matcher, bot: Bot, event: Event, args=CommandArg()):
    qq_bot = _resolve_qq_bot(bot)
    if qq_bot is None:
        await matcher.finish(
            "未找到已连接的官方 QQ Bot，无法配置菜单喵～\n"
            "请确认已启用 nonebot-adapter-qq 并成功上线。"
        )
    try:
        result = await qm.apply_default_menu(qq_bot)
    except Exception as exc:
        log.warning(f"[qq-menu] 设置菜单失败: {exc!r}")
        await matcher.finish(_format_api_error(exc))
    version = (result or {}).get("version", "?")
    await matcher.finish(
        f"✅ 已设置 C2C 自定义菜单（{len(qm.default_menu_items())} 个按钮，version={version}）。\n"
        "菜单仅在单聊窗口底部展示，可能需要重新进入聊天才会刷新喵～"
    )


@get_menu_cmd.handle()
async def _(matcher: Matcher, bot: Bot, event: Event):
    qq_bot = _resolve_qq_bot(bot)
    if qq_bot is None:
        await matcher.finish("未找到已连接的官方 QQ Bot。")
    try:
        data = await qm.get_menu(qq_bot)
    except Exception as exc:
        await matcher.finish(_format_api_error(exc))
    menu = (data or {}).get("menu")
    version = (data or {}).get("version", "?")
    if not menu or not menu.get("items"):
        await matcher.finish(f"当前未设置自定义菜单（version={version}）。")
    lines = [f"当前自定义菜单（version={version}）："]
    for idx, item in enumerate(menu.get("items", []), 1):
        name = item.get("name", "")
        item_type = item.get("type", "")
        if item_type == "menu":
            subs = " / ".join(
                s.get("name", "") for s in item.get("sub_menu_items", [])
            )
            lines.append(f"{idx}. {name} [折叠菜单] → {subs}")
        elif item_type == "send_message":
            lines.append(
                f"{idx}. {name} [发送] → {item.get('send_message', '')}"
            )
        elif item_type == "link":
            lines.append(f"{idx}. {name} [链接] → {item.get('link', '')}")
        elif item_type == "switch":
            switch = item.get("switch") or {}
            lines.append(
                f"{idx}. {name} [开关] id={switch.get('switch_id', '')} "
                f"default={switch.get('default', False)}"
            )
        else:
            lines.append(f"{idx}. {name} [{item_type}]")
    await matcher.finish("\n".join(lines))


@set_panel_cmd.handle()
async def _(matcher: Matcher, bot: Bot, event: Event, args=CommandArg()):
    qq_bot = _resolve_qq_bot(bot)
    if qq_bot is None:
        await matcher.finish("未找到已连接的官方 QQ Bot，无法配置面板。")
    scope_arg = _plain_arg(args).lower()
    if scope_arg in ("c2c", "单聊", "私聊"):
        scopes = ("c2c",)
    elif scope_arg in ("group", "群聊", "群"):
        scopes = ("group",)
    else:
        scopes = ("c2c", "group")
    try:
        result = await qm.apply_default_panels(qq_bot, scopes=scopes)
    except Exception as exc:
        log.warning(f"[qq-menu] 设置面板失败: {exc!r}")
        await matcher.finish(_format_api_error(exc))
    if not result:
        await matcher.finish("没有可设置的面板（当前默认面板仅支持 c2c / group）。")
    lines = ["✅ 指令面板已同步："]
    for scope, panel_id in result.items():
        lines.append(f"• {scope}: {panel_id}")
    lines.append("面板在对应场景的输入框「+」面板中展示，可能需要重新进入聊天刷新喵～")
    await matcher.finish("\n".join(lines))


@list_panel_cmd.handle()
async def _(matcher: Matcher, bot: Bot, event: Event, args=CommandArg()):
    qq_bot = _resolve_qq_bot(bot)
    if qq_bot is None:
        await matcher.finish("未找到已连接的官方 QQ Bot。")
    scope = _plain_arg(args).lower() or "c2c"
    if scope in ("单聊", "私聊"):
        scope = "c2c"
    elif scope in ("群聊", "群"):
        scope = "group"
    if scope not in qm.VALID_SCOPES:
        await matcher.finish(
            f"scope 仅支持 {', '.join(qm.VALID_SCOPES)}（或 单聊/群聊）。"
        )
    try:
        records = await qm.iter_all_panels(qq_bot, scope)
    except Exception as exc:
        await matcher.finish(_format_api_error(exc))
    if not records:
        await matcher.finish(f"scope={scope} 下暂无指令面板。")
    lines = [f"指令面板列表（scope={scope}，共 {len(records)} 个）："]
    for rec in records:
        panel = rec.get("panel") or {}
        count = len(panel.get("items") or [])
        remark = panel.get("remark") or ""
        lines.append(
            f"• {rec.get('panel_id', '')} | "
            f"{rec.get('target_type', '')} | "
            f"{count} 项 | v{rec.get('version', '?')}"
            + (f" | {remark}" if remark else "")
        )
    lines.append("用「QQ面板详情 <panel_id>」查看具体内容。")
    await matcher.finish("\n".join(lines))


@panel_detail_cmd.handle()
async def _(matcher: Matcher, bot: Bot, event: Event, args=CommandArg()):
    panel_id = _plain_arg(args)
    if not panel_id:
        await matcher.finish("用法：QQ面板详情 <panel_id>")
    qq_bot = _resolve_qq_bot(bot)
    if qq_bot is None:
        await matcher.finish("未找到已连接的官方 QQ Bot。")
    try:
        detail = await qm.get_panel(qq_bot, panel_id)
    except Exception as exc:
        await matcher.finish(_format_api_error(exc))
    panel = detail.get("panel") or {}
    lines = [
        f"面板 {detail.get('panel_id', panel_id)}",
        f"scope={detail.get('scope', '')} | "
        f"target_type={detail.get('target_type', '')} | "
        f"v{detail.get('version', '?')}",
    ]
    remark = panel.get("remark")
    if remark:
        lines.append(f"remark: {remark}")
    lines.append("元素：")
    for idx, item in enumerate(panel.get("items", []), 1):
        if item.get("type") == "link":
            lines.append(
                f"  {idx}. {item.get('name', '')} — "
                f"{item.get('desc', '')} [链接] {item.get('link', '')}"
            )
        else:
            admin_flag = " (仅管理员)" if item.get("only_admin") else ""
            lines.append(
                f"  {idx}. {item.get('name', '')} — "
                f"{item.get('desc', '')}{admin_flag}"
            )
    if detail.get("group_openids"):
        lines.append(
            f"关联群: {', '.join(detail['group_openids'][:10])}"
            + (" …" if len(detail["group_openids"]) > 10 else "")
        )
    if detail.get("user_openids"):
        lines.append(
            f"关联用户: {', '.join(detail['user_openids'][:10])}"
            + (" …" if len(detail["user_openids"]) > 10 else "")
        )
    await matcher.finish("\n".join(lines))


@delete_panel_cmd.handle()
async def _(matcher: Matcher, bot: Bot, event: Event, args=CommandArg()):
    panel_id = _plain_arg(args)
    if not panel_id:
        await matcher.finish("用法：删除QQ面板 <panel_id>")
    qq_bot = _resolve_qq_bot(bot)
    if qq_bot is None:
        await matcher.finish("未找到已连接的官方 QQ Bot。")
    try:
        await qm.delete_panel(qq_bot, panel_id)
    except Exception as exc:
        await matcher.finish(_format_api_error(exc))
    await matcher.finish(f"🗑️ 已删除面板 {panel_id}。")
