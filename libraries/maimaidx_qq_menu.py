"""QQ 官方机器人「自定义菜单 / 指令面板」OpenAPI 客户端与默认配置。

文档: https://bot.q.qq.com/wiki/develop/api-v2/server-inter/menu-panel/

- 自定义菜单 (PUT/GET /v2/menu)：仅 C2C 单聊场景，底部常驻菜单条，全局生效。
- 指令面板 (/v2/panels)：c2c / group / channel / dm 四种场景，可全局或按
  指定用户/群生效。

所有 HTTP 请求复用 ``nonebot-adapter-qq`` 已维护的 app access_token
(``Bot.get_authorization_header``)，并借适配器的 HTTP driver 发出，
不自行管理 token 生命周期，不硬编码任何生产凭据。
"""

from __future__ import annotations

import asyncio

from typing import Any, Optional

from nonebot.drivers import Request

from ..config import log
from .maimaidx_platform import is_qq_bot

# ---- 常量 ----------------------------------------------------------------

MENU_API_BASE = "/v2/menu"
PANELS_API_BASE = "/v2/panels"

VALID_SCOPES = ("c2c", "group", "channel", "dm")
VALID_TARGET_TYPES = ("all", "specific")
VALID_MENU_TYPES = ("switch", "send_message", "link", "menu")
VALID_PANEL_ITEM_TYPES = ("command", "link")

# 字符宽度限制（一个中文汉字算 2 个字符，按文档口径）
_MENU_NAME_LIMIT = 10
_SUB_MENU_NAME_LIMIT = 14
_PANEL_NAME_LIMIT = 14
_PANEL_DESC_LIMIT = 30
_PANEL_REMARK_LIMIT = 255
_MAX_MENU_ITEMS = 10
_MAX_SUB_MENU_ITEMS = 5
_MAX_PANEL_ITEMS = 20


def _char_width(text: str) -> int:
    """按 QQ 文档口径计算字符宽度：非 ASCII 算 2。"""
    width = 0
    for ch in str(text):
        width += 2 if ord(ch) > 127 else 1
    return width


def _truncate(text: str, limit: int) -> str:
    """按宽度截断，超出部分丢弃（不抛错，保证配置可用）。"""
    if _char_width(text) <= limit:
        return text
    out = ""
    width = 0
    for ch in str(text):
        w = 2 if ord(ch) > 127 else 1
        if width + w > limit:
            break
        out += ch
        width += w
    return out


# ---- 低层 HTTP 封装 -------------------------------------------------------


async def _qq_api_request(
    bot: Any,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json: Any = None,
) -> Any:
    """通过官方 QQ 适配器发送已鉴权的 OpenAPI 请求。

    复用 ``Bot._request`` 以获得 access_token 注入、401 自动刷新与统一的
    异常映射（``ActionFailed`` / ``RateLimitException`` 等）。
    """
    if not is_qq_bot(bot):
        raise TypeError("当前 Bot 不是官方 QQ 适配器，无法调用菜单/面板 API")

    url = bot.adapter.get_api_base().joinpath(path.lstrip("/"))
    request = Request(method.upper(), url, params=params, json=json)

    if hasattr(bot, "_request"):
        return await bot._request(request)

    request.headers.update(await bot.get_authorization_header())
    response = await bot.adapter.request(request)
    return _parse_response(response)


def _parse_response(response: Any) -> Any:
    """``_request`` 不可用时的最小响应解析兜底。"""
    status = getattr(response, "status_code", None) or getattr(
        response, "status", None
    )
    content = getattr(response, "content", None)
    if status == 204 or not content:
        return {}
    import json as _json

    try:
        data = _json.loads(content)
    except Exception:
        return {"raw": content}
    if status and status >= 400:
        raise RuntimeError(f"QQ OpenAPI 错误 ({status}): {data}")
    return data


# ---- 自定义菜单 -----------------------------------------------------------


async def get_menu(bot: Any) -> dict:
    """查询当前全局自定义菜单。未设置时 menu 字段为空。"""
    return await _qq_api_request(bot, "GET", MENU_API_BASE)


async def set_menu(bot: Any, items: list[dict]) -> dict:
    """覆盖设置全局自定义菜单（C2C）。

    :param items: MenuItem 列表，最多 10 个。
    """
    _validate_menu_items(items)
    body = {"menu": {"items": items}}
    return await _qq_api_request(bot, "PUT", MENU_API_BASE, json=body)


def _validate_menu_items(items: list[dict]) -> None:
    if not isinstance(items, list):
        raise ValueError("menu items 必须是列表")
    if len(items) > _MAX_MENU_ITEMS:
        raise ValueError(f"菜单项最多 {_MAX_MENU_ITEMS} 个")
    for item in items:
        item_type = item.get("type")
        if item_type not in VALID_MENU_TYPES:
            raise ValueError(f"非法菜单类型: {item_type}")
        name = item.get("name", "")
        if _char_width(name) > _MENU_NAME_LIMIT:
            item["name"] = _truncate(name, _MENU_NAME_LIMIT)
        if item_type == "menu":
            subs = item.get("sub_menu_items") or []
            if len(subs) > _MAX_SUB_MENU_ITEMS:
                raise ValueError(f"子菜单最多 {_MAX_SUB_MENU_ITEMS} 个")
            for sub in subs:
                if sub.get("type") not in ("send_message", "link"):
                    raise ValueError("二级菜单仅支持 send_message/link")
                if _char_width(sub.get("name", "")) > _SUB_MENU_NAME_LIMIT:
                    sub["name"] = _truncate(
                        sub.get("name", ""), _SUB_MENU_NAME_LIMIT
                    )
                if sub.get("type") == "link" and not str(
                    sub.get("link", "")
                ).startswith("https://"):
                    raise ValueError("link 必须以 https:// 开头")
        elif item_type == "link":
            if not str(item.get("link", "")).startswith("https://"):
                raise ValueError("link 必须以 https:// 开头")
        elif item_type == "switch":
            switch = item.get("switch") or {}
            if not switch.get("switch_id"):
                raise ValueError("switch 类型必须提供 switch_id")


# ---- 指令面板 -------------------------------------------------------------


async def list_panels(
    bot: Any,
    scope: str,
    *,
    cursor: str = "",
    limit: int = 20,
) -> dict:
    """分页查询指定场景下的指令面板列表。"""
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope 仅支持 {VALID_SCOPES}")
    params: dict[str, Any] = {"scope": scope, "limit": min(max(1, limit), 50)}
    if cursor:
        params["cursor"] = cursor
    return await _qq_api_request(bot, "GET", PANELS_API_BASE, params=params)


async def iter_all_panels(bot: Any, scope: str) -> list[dict]:
    """拉取指定场景的全部面板（自动翻页）。"""
    records: list[dict] = []
    cursor = ""
    while True:
        page = await list_panels(bot, scope, cursor=cursor, limit=50)
        records.extend(page.get("records") or [])
        if page.get("is_end"):
            break
        cursor = page.get("next_cursor") or ""
        if not cursor:
            break
    return records


async def create_panel(
    bot: Any,
    scope: str,
    items: list[dict],
    *,
    target_type: str = "all",
    user_openids: Optional[list[str]] = None,
    group_openids: Optional[list[str]] = None,
    remark: str = "",
) -> dict:
    """创建指令面板，返回 ``{"panel_id": "..."}``。"""
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope 仅支持 {VALID_SCOPES}")
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"target_type 仅支持 {VALID_TARGET_TYPES}")
    if scope in ("channel", "dm") and target_type != "all":
        raise ValueError("channel/dm 场景仅支持 target_type=all")
    _validate_panel_items(items)

    body: dict[str, Any] = {
        "scope": scope,
        "target_type": target_type,
        "panel": {"items": items},
    }
    if remark:
        body["panel"]["remark"] = _truncate(remark, _PANEL_REMARK_LIMIT)
    if target_type == "specific":
        if scope == "c2c" and user_openids:
            body["user_openids"] = user_openids[:20]
        if scope == "group" and group_openids:
            body["group_openids"] = group_openids[:20]
    return await _qq_api_request(bot, "POST", PANELS_API_BASE, json=body)


async def get_panel(bot: Any, panel_id: str) -> dict:
    """查询指令面板详情。"""
    return await _qq_api_request(
        bot, "GET", f"{PANELS_API_BASE}/{panel_id}"
    )


async def update_panel(
    bot: Any,
    panel_id: str,
    items: list[dict],
    *,
    remark: Optional[str] = None,
) -> dict:
    """修改指令面板元素（整体覆盖）。"""
    _validate_panel_items(items)
    panel: dict[str, Any] = {"items": items}
    if remark is not None:
        panel["remark"] = _truncate(remark, _PANEL_REMARK_LIMIT)
    body = {"panel": panel}
    return await _qq_api_request(
        bot, "PUT", f"{PANELS_API_BASE}/{panel_id}", json=body
    )


async def delete_panel(bot: Any, panel_id: str) -> dict:
    """删除指令面板。"""
    return await _qq_api_request(
        bot, "DELETE", f"{PANELS_API_BASE}/{panel_id}"
    )


async def update_panel_target(
    bot: Any,
    panel_id: str,
    op: str,
    *,
    user_openids: Optional[list[str]] = None,
    group_openids: Optional[list[str]] = None,
) -> dict:
    """添加/移除指令面板关联的用户或群（仅 c2c/group + specific）。"""
    if op not in ("add", "del"):
        raise ValueError("op 仅支持 add/del")
    body: dict[str, Any] = {"op": op}
    if user_openids:
        body["user_openids"] = user_openids[:20]
    if group_openids:
        body["group_openids"] = group_openids[:20]
    return await _qq_api_request(
        bot,
        "PUT",
        f"{PANELS_API_BASE}/{panel_id}/target",
        json=body,
    )


def _validate_panel_items(items: list[dict]) -> None:
    if not isinstance(items, list) or not items:
        raise ValueError("panel items 必须是非空列表")
    if len(items) > _MAX_PANEL_ITEMS:
        raise ValueError(f"面板元素最多 {_MAX_PANEL_ITEMS} 个")
    for item in items:
        item_type = item.get("type")
        if item_type not in VALID_PANEL_ITEM_TYPES:
            raise ValueError(f"非法面板元素类型: {item_type}")
        if _char_width(item.get("name", "")) > _PANEL_NAME_LIMIT:
            item["name"] = _truncate(
                item.get("name", ""), _PANEL_NAME_LIMIT
            )
        if item.get("desc") and _char_width(item["desc"]) > _PANEL_DESC_LIMIT:
            item["desc"] = _truncate(item["desc"], _PANEL_DESC_LIMIT)
        if item_type == "link":
            if not str(item.get("link", "")).startswith("https://"):
                raise ValueError("link 必须以 https:// 开头")


# ---- 默认配置 -------------------------------------------------------------


def default_menu_items() -> list[dict]:
    """C2C 底部自定义菜单默认项（与现有指令对齐）。"""
    return [
        {"type": "send_message", "name": "帮助", "send_message": "帮助"},
        {"type": "send_message", "name": "B50", "send_message": "b50"},
        {"type": "send_message", "name": "绑定", "send_message": "mai绑定"},
        {"type": "send_message", "name": "签到", "send_message": "签到"},
        {"type": "send_message", "name": "查歌", "send_message": "查歌"},
        {
            "type": "menu",
            "name": "猜歌",
            "sub_menu_items": [
                {"type": "send_message", "name": "猜歌", "send_message": "猜歌"},
                {
                    "type": "send_message",
                    "name": "猜曲绘",
                    "send_message": "猜封面",
                },
                {
                    "type": "send_message",
                    "name": "猜曲子",
                    "send_message": "猜曲子",
                },
                {
                    "type": "send_message",
                    "name": "猜谱面",
                    "send_message": "猜谱面",
                },
            ],
        },
        {
            "type": "send_message",
            "name": "吃分推荐",
            "send_message": "吃分推荐",
        },
        {
            "type": "send_message",
            "name": "今日mai",
            "send_message": "今日mai",
        },
    ]


def default_c2c_panel_items() -> list[dict]:
    """C2C 指令面板默认项（单聊：查分 / 账号 / 个人分析为主）。"""
    return [
        {"type": "command", "name": "b50", "desc": "查询 B50 成绩"},
        {"type": "command", "name": "刷新b50", "desc": "刷新最新成绩"},
        {"type": "command", "name": "锐评一下", "desc": "AI 锐评 B50"},
        {"type": "command", "name": "吃分推荐", "desc": "个性化推分"},
        {"type": "command", "name": "mymai", "desc": "我的舞萌资料"},
        {"type": "command", "name": "mai绑定", "desc": "绑定舞萌账号"},
        {"type": "command", "name": "签到", "desc": "每日签到"},
        {"type": "command", "name": "查歌", "desc": "搜索曲目"},
        {"type": "command", "name": "今日mai", "desc": "今日运势"},
        {"type": "command", "name": "我有多菜", "desc": "弱项分析"},
        {"type": "command", "name": "含金量", "desc": "成绩含金量"},
        {"type": "command", "name": "b50鸟率", "desc": "鸟/鸟加率统计"},
        {"type": "command", "name": "我的AWMC", "desc": "我的 BREAK 资产"},
        {"type": "command", "name": "帮助", "desc": "查看完整帮助"},
    ]


def default_group_panel_items() -> list[dict]:
    """Group 指令面板默认项（群聊：查分 / 猜歌 / 排行 / 签到）。"""
    return [
        {"type": "command", "name": "b50", "desc": "查询 B50 成绩"},
        {"type": "command", "name": "锐评一下", "desc": "AI 锐评 B50"},
        {"type": "command", "name": "签到", "desc": "每日签到"},
        {"type": "command", "name": "猜歌", "desc": "开始猜歌"},
        {"type": "command", "name": "猜封面", "desc": "开始猜曲绘"},
        {"type": "command", "name": "猜曲子", "desc": "听音猜曲"},
        {"type": "command", "name": "猜谱面", "desc": "看谱猜曲"},
        {"type": "command", "name": "查歌", "desc": "搜索曲目"},
        {"type": "command", "name": "吃分推荐", "desc": "个性化推分"},
        {"type": "command", "name": "今日mai", "desc": "今日运势"},
        {"type": "command", "name": "猜歌积分排行", "desc": "查看排行榜"},
        {"type": "command", "name": "本群猜歌排行", "desc": "群内排行"},
        {"type": "command", "name": "我的AWMC", "desc": "我的 BREAK 资产"},
        {"type": "command", "name": "帮助", "desc": "查看完整帮助"},
    ]


# ---- 一键应用（幂等） -----------------------------------------------------


async def apply_default_menu(bot: Any) -> dict:
    """幂等设置默认 C2C 自定义菜单，返回 API 响应。"""
    items = default_menu_items()
    log.info("[qq-menu] 正在设置默认自定义菜单（C2C）…")
    result = await set_menu(bot, items)
    log.success(f"[qq-menu] 自定义菜单设置成功: {result}")
    return result


async def apply_default_panels(bot: Any, *, scopes: tuple[str, ...] = ("c2c", "group")) -> dict:
    """幂等创建/更新默认指令面板。

    对每个 scope：若已存在由本插件创建（remark 以 ``maimaidx:`` 开头）的
    全局面板，则更新它；否则创建一个新的。返回 ``{scope: panel_id}``。
    """
    result: dict[str, str] = {}
    builders = {
        "c2c": (default_c2c_panel_items, "maimaidx:c2c:default"),
        "group": (default_group_panel_items, "maimaidx:group:default"),
    }
    for scope in scopes:
        if scope not in ("c2c", "group"):
            log.warning(f"[qq-menu] 默认面板暂不支持 scope={scope}，跳过")
            continue
        items_builder, remark = builders[scope]
        try:
            existing = await iter_all_panels(bot, scope)
            target = next(
                (
                    p
                    for p in existing
                    if (p.get("panel") or {}).get("remark") == remark
                    and p.get("target_type") == "all"
                ),
                None,
            )
            if target:
                panel_id = target["panel_id"]
                await update_panel(bot, panel_id, items_builder(), remark=remark)
                log.info(f"[qq-menu] 已更新 {scope} 默认面板 {panel_id}")
            else:
                created = await create_panel(
                    bot,
                    scope,
                    items_builder(),
                    target_type="all",
                    remark=remark,
                )
                panel_id = created.get("panel_id", "")
                log.success(f"[qq-menu] 已创建 {scope} 默认面板 {panel_id}")
            result[scope] = panel_id
        except Exception as exc:
            log.warning(f"[qq-menu] {scope} 面板同步失败（不影响其它面板）: {exc}")
    return result


async def safe_auto_setup(bot: Any) -> bool:
    """启动时自动配置菜单/面板的容错入口。

    菜单与面板互相独立；任一失败都只记日志、不影响 Bot 启动，也不影响
    另一项。返回 True 表示全部成功（供调用方决定是否需要下次重连重试）。
    """
    if not is_qq_bot(bot):
        return True
    ok = True
    try:
        await apply_default_menu(bot)
    except asyncio.CancelledError:
        log.warning("[qq-menu] 菜单同步被取消（可能是网络断连），下次重连重试")
        ok = False
    except Exception as exc:
        log.warning(f"[qq-menu] 菜单同步失败（不影响启动）: {exc}")
        ok = False
    try:
        panels = await apply_default_panels(bot)
        if {"c2c", "group"} != set(panels):
            ok = False
    except asyncio.CancelledError:
        log.warning("[qq-menu] 面板同步被取消（可能是网络断连），下次重连重试")
        ok = False
    except Exception as exc:
        log.warning(f"[qq-menu] 面板同步失败（不影响启动）: {exc}")
        ok = False
    return ok
