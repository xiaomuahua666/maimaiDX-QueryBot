"""插件管理员：合并 NoneBot SUPERUSER 与 .env 配置的 platform id。"""

from __future__ import annotations

import re
from typing import Set, Union

from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.permission import Permission

from ..config import maiconfig


def _parse_id_list(raw: str) -> Set[str]:
    if not raw:
        return set()
    parts = re.split(r'[,\s;|]+', str(raw).strip())
    return {p.strip() for p in parts if p.strip()}


def get_plugin_admin_ids() -> Set[str]:
    """NoneBot superusers ∪ MAIMAIDX_BOT_ADMINS。"""
    ids: Set[str] = set()
    ids.update(_parse_id_list(getattr(maiconfig, 'maimaidx_bot_admins', '') or ''))
    try:
        from nonebot import get_driver
        ids.update(str(u) for u in get_driver().config.superusers)
    except Exception:
        pass
    return ids


def is_plugin_admin(user_id: Union[int, str]) -> bool:
    raw = str(user_id).strip()
    admin_ids = get_plugin_admin_ids()
    if raw in admin_ids:
        return True
    if not raw or raw.isdigit():
        return False
    try:
        from .maimaidx_qq_bind import qq_bind_db

        legacy_qq = qq_bind_db.get_legacy_qq(raw)
        if legacy_qq is None:
            forum = qq_bind_db.get_forum_binding(raw) or {}
            legacy_qq = forum.get('legacy_qq')
        return legacy_qq is not None and str(int(legacy_qq)) in admin_ids
    except Exception:
        # Permission checks must fail closed if the optional binding store is
        # unavailable or malformed; a database hiccup must not crash dispatch.
        return False


def _qq_group_role(event) -> str | None:
    """官方 QQ 群消息中的 member_role：owner / admin / member。"""
    author = getattr(event, 'author', None)
    if author is None:
        return None
    role = getattr(author, 'member_role', None)
    if role:
        return str(role).lower()
    if isinstance(author, dict):
        r = author.get('member_role')
        return str(r).lower() if r else None
    return None


def is_qq_group_manager(event) -> bool:
    return _qq_group_role(event) in ('owner', 'admin')


def _onebot_group_manager(event) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    role = getattr(getattr(event, 'sender', None), 'role', None)
    return role in ('owner', 'admin')


async def _group_manager_or_plugin_admin(event) -> bool:
    if is_plugin_admin(event.get_user_id()):
        return True
    if _onebot_group_manager(event):
        return True
    if is_qq_group_manager(event):
        return True
    return False


async def _plugin_admin_only(event) -> bool:
    return is_plugin_admin(event.get_user_id())


# 猜歌群管 / 插件管理员（兼容 OneBot 群管 + 官方 QQ owner/admin）
GUESS_GROUP_MANAGER = Permission(_group_manager_or_plugin_admin)

# 仅插件管理员（含 env 与 superuser）
PLUGIN_ADMIN_ONLY = Permission(_plugin_admin_only)
