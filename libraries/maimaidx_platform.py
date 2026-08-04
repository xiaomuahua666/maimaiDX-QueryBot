"""平台适配：OneBot / 官方 QQ，查分 QQ 解析与消息形态。"""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union

from nonebot.adapters.onebot.v11 import Message, MessageSegment

GroupId = Union[int, str]
UserId = Union[int, str]

from ..config import log, maiconfig
from .maimaidx_qq_bind import qq_bind_db


def install_qq_event_compat() -> None:
    """Expose the OneBot-style aliases used by older command modules.

    ``nonebot-adapter-qq`` deliberately exposes ``id``/``author`` while many
    mature plugin commands still read ``message_id``/``user_id``/``sender``.
    Adding read-only aliases keeps both adapters usable without changing the
    encrypted values or pretending they are real QQ numbers.
    """
    try:
        from nonebot.adapters.qq.event import QQMessageEvent
        from nonebot.adapters.qq.models import GroupMemberAuthor
    except (ImportError, AttributeError):
        return

    # Adapter 1.7.x does not declare ``member_role`` on GroupMemberAuthor and
    # therefore drops the field from Tencent's payload.  Keep unknown fields so
    # group owner/admin checks can use the role when the platform sends it.
    try:
        GroupMemberAuthor.__annotations__["member_role"] = str | None
        GroupMemberAuthor.model_config["extra"] = "allow"
        GroupMemberAuthor.model_rebuild(force=True)
        author_aliases = {
            "role": property(
                lambda author: getattr(author, "member_role", None) or "member"
            ),
            "nickname": property(
                lambda author: getattr(author, "username", None) or ""
            ),
            "card": property(lambda author: getattr(author, "username", None) or ""),
            "user_id": property(
                lambda author: getattr(author, "member_openid", None)
                or getattr(author, "id", "")
            ),
        }
        for name, value in author_aliases.items():
            if not hasattr(GroupMemberAuthor, name):
                setattr(GroupMemberAuthor, name, value)
    except Exception as exc:
        log.debug(f"[platform] QQ 作者字段兼容补丁安装失败: {exc}")

    aliases = {
        'user_id': property(lambda event: event.get_user_id()),
        'message_id': property(lambda event: str(getattr(event, 'id', ''))),
        'sender': property(lambda event: getattr(event, 'author', None)),
    }
    for name, value in aliases.items():
        if not hasattr(QQMessageEvent, name):
            try:
                setattr(QQMessageEvent, name, value)
            except Exception:
                log.debug(f'[platform] 无法安装官方 QQ 事件兼容属性 {name}')

    # Older commands use ``event.group_id`` as the delivery key.  Official QQ
    # exposes both a legacy group id and the API-facing group_openid; normalize
    # the former to the latter so those calls never accidentally send an
    # encrypted/legacy value to the QQ API.
    try:
        original_getattribute = QQMessageEvent.__getattribute__
        if not getattr(original_getattribute, '_maimaidx_qq_group_id', False):
            def _qq_getattribute(event, name):
                if name == 'group_id':
                    try:
                        openid = original_getattribute(event, 'group_openid')
                    except AttributeError:
                        openid = None
                    if openid is not None:
                        return openid
                return original_getattribute(event, name)

            _qq_getattribute._maimaidx_qq_group_id = True
            QQMessageEvent.__getattribute__ = _qq_getattribute
    except (AttributeError, TypeError):
        pass

    # A number of mature commands use ``isinstance(event,
    # onebot.GroupMessageEvent)`` for feature switches.  Make those checks
    # understand QQ group/C2C events as well; the hook is restricted to the
    # three OneBot event classes and delegates every other class unchanged.
    try:
        from nonebot.adapters.onebot.v11.event import (
            GroupMessageEvent as OneBotGroupMessageEvent,
            MessageEvent as OneBotMessageEvent,
            PrivateMessageEvent as OneBotPrivateMessageEvent,
        )

        event_meta = type(OneBotMessageEvent)
        if not getattr(event_meta, '_maimaidx_qq_instancecheck', False):
            original_instancecheck = event_meta.__instancecheck__

            def _qq_aware_instancecheck(cls, value):
                module = type(value).__module__
                if module.startswith('nonebot.adapters.qq'):
                    if cls is OneBotGroupMessageEvent:
                        return getattr(value, 'group_openid', None) is not None
                    if cls is OneBotPrivateMessageEvent:
                        return getattr(value, 'group_openid', None) is None
                    if cls is OneBotMessageEvent:
                        return hasattr(value, 'get_message')
                return original_instancecheck(cls, value)

            event_meta.__instancecheck__ = _qq_aware_instancecheck
            event_meta._maimaidx_qq_instancecheck = True
    except (ImportError, AttributeError):
        pass

    # Most of the historical command modules annotate their dependencies with
    # OneBot's ``MessageEvent``/``Message``/``Bot`` classes.  NoneBot validates
    # those annotations before invoking a handler, so a QQ event would never
    # reach the platform-aware code below.  Keep the old annotations working by
    # relaxing only the adapter boundary: a value from ``nonebot.adapters.qq``
    # is accepted for an annotation from ``nonebot.adapters.onebot``.  Other
    # type mismatches still use NoneBot's normal validation and continue to
    # fail fast.
    try:
        import nonebot.dependencies.utils as dependency_utils
        import nonebot.internal.params as internal_params

        original_check = dependency_utils.check_field_type
        if not getattr(original_check, '_maimaidx_qq_compat', False):
            def _check_field_type(field, value):
                expected = getattr(field, 'annotation', None)
                expected_module = getattr(expected, '__module__', '')
                value_module = type(value).__module__
                if (
                    expected_module.startswith('nonebot.adapters.onebot')
                    and value_module.startswith('nonebot.adapters.qq')
                ):
                    return value
                return original_check(field, value)

            _check_field_type._maimaidx_qq_compat = True
            dependency_utils.check_field_type = _check_field_type
            # ``nonebot.internal.params`` keeps a module-level reference imported
            # from dependency_utils, so patch it as well for already-loaded classes.
            internal_params.check_field_type = _check_field_type
    except Exception as exc:
        log.warning(f'[platform] 官方 QQ 依赖注入兼容补丁安装失败: {exc}')

    try:
        from nonebot.matcher import Matcher, current_event

        original_matcher_send = Matcher.send
        if not getattr(original_matcher_send, '_maimaidx_qq_compat', False):
            async def _qq_aware_matcher_send(cls, message, **kwargs):
                event = current_event.get(None)
                if is_qq_event(event) and _is_onebot_payload(message):
                    message = adapt_guess_outbound(message, event=event)
                return await original_matcher_send(message, **kwargs)

            _qq_aware_matcher_send._maimaidx_qq_compat = True
            Matcher.send = classmethod(_qq_aware_matcher_send)
    except Exception as exc:
        log.warning(f'[platform] 官方 QQ Matcher.send 兼容补丁安装失败: {exc}')

    try:
        from nonebot.adapters.qq.bot import Bot as QQBot

        original_qq_send = QQBot.send
        if not getattr(original_qq_send, '_maimaidx_qq_compat', False):
            async def _qq_aware_bot_send(self, event, message, **kwargs):
                if is_qq_event(event) and _is_onebot_payload(message):
                    message = adapt_guess_outbound(message, event=event)
                return await original_qq_send(self, event, message, **kwargs)

            _qq_aware_bot_send._maimaidx_qq_compat = True
            QQBot.send = _qq_aware_bot_send

        original_qq_call_api = QQBot.call_api
        if not getattr(original_qq_call_api, '_maimaidx_qq_compat', False):
            async def _qq_aware_call_api(self, api: str, **data):
                """Translate the small OneBot API subset used by this plugin."""
                api_name = str(api)
                if api_name in ('send_group_msg', 'send_private_msg'):
                    message = data.get('message', '')
                    if api_name == 'send_group_msg':
                        return await self.send_to_group(
                            group_openid=str(data.get('group_id', '')),
                            message=adapt_guess_outbound(message),
                        )
                    return await self.send_to_c2c(
                        openid=str(data.get('user_id', '')),
                        message=adapt_guess_outbound(message),
                    )
                if api_name in ('send_group_forward_msg', 'send_private_forward_msg'):
                    title = '合并转发'
                    text = format_forward_nodes_as_text(
                        title, data.get('messages') or data.get('nodes') or []
                    )
                    if api_name == 'send_group_forward_msg':
                        return await self.send_to_group(
                            group_openid=str(data.get('group_id', '')), message=text
                        )
                    return await self.send_to_c2c(
                        openid=str(data.get('user_id', '')), message=text
                    )
                if api_name == 'delete_msg':
                    message_id = str(data.get('message_id', ''))
                    if data.get('group_id'):
                        return await self.delete_group_message(
                            group_openid=str(data['group_id']), message_id=message_id
                        )
                    if data.get('user_id'):
                        return await self.delete_c2c_message(
                            openid=str(data['user_id']), message_id=message_id
                        )
                    # Official QQ has no global delete-by-message-id endpoint.
                    return None
                if api_name in ('get_group_member_list', 'get_group_member_info'):
                    # The official API does not expose QQ numbers.  Callers that
                    # need an identity use qq_member_registry/qbind instead.
                    return [] if api_name.endswith('list') else {}
                return await original_qq_call_api(self, api_name, **data)

            _qq_aware_call_api._maimaidx_qq_compat = True
            QQBot.call_api = _qq_aware_call_api

        # A few legacy modules call these OneBot convenience methods directly.
        # Add narrow shims instead of changing the public QQ adapter package.
        if not hasattr(QQBot, 'send_group_msg'):
            async def _send_group_msg(self, *, group_id, message, **kwargs):
                return await self.send_to_group(
                    group_openid=str(group_id), message=adapt_guess_outbound(message)
                )
            QQBot.send_group_msg = _send_group_msg
        if not hasattr(QQBot, 'send_private_msg'):
            async def _send_private_msg(self, *, user_id, message, **kwargs):
                return await self.send_to_c2c(
                    openid=str(user_id), message=adapt_guess_outbound(message)
                )
            QQBot.send_private_msg = _send_private_msg
        if not hasattr(QQBot, 'delete_msg'):
            async def _delete_msg(self, *, message_id, **kwargs):
                return await self.call_api('delete_msg', message_id=message_id, **kwargs)
            QQBot.delete_msg = _delete_msg
    except (ImportError, AttributeError):
        pass


def _is_onebot_payload(value: Any) -> bool:
    """Whether a message object was constructed by the OneBot adapter."""
    module = type(value).__module__
    return module.startswith('nonebot.adapters.onebot')


def get_platform() -> str:
    """onebot | qq_official（.env 默认倾向，可被事件来源覆盖）。"""
    raw = (getattr(maiconfig, 'maimaidx_platform', None) or 'onebot').strip().lower()
    if raw in ('qq', 'qq_official', 'official', 'qqbot'):
        return 'qq_official'
    return 'onebot'


def is_qq_official() -> bool:
    return get_platform() == 'qq_official'


def is_qq_event(event) -> bool:
    """按事件类型判断：官方 QQ 群/私聊消息。"""
    if event is None:
        return False
    mod = type(event).__module__
    return mod.startswith('nonebot.adapters.qq')


def use_qq_mode(event=None) -> bool:
    """
    是否按官方 QQ 逻辑处理。
    同一进程挂 OneBot + QQ 时，以事件来源为准；无 event 时回退 .env。
    """
    if event is not None:
        if is_qq_event(event):
            return True
        mod = type(event).__module__
        if mod.startswith('nonebot.adapters.onebot'):
            return False
    return is_qq_official()


def use_qq_card_message(event=None) -> bool:
    return bool(getattr(maiconfig, 'maimaidx_use_qq_card', False)) and use_qq_mode(event)


def get_event_group_id(event) -> Optional[GroupId]:
    """OneBot group_id 或官方 QQ group_openid。"""
    if event is None:
        return None
    openid = getattr(event, 'group_openid', None)
    if openid is not None:
        return str(openid)
    gid = getattr(event, 'group_id', None)
    if gid is not None:
        return gid
    return None


def resolve_group_legacy_id(group_id: Optional[GroupId]) -> Optional[int]:
    """Return an administrator-configured old QQ group number, if any."""
    if group_id is None:
        return None
    return qq_bind_db.get_group_legacy_id(str(group_id))


def event_group_data_id(event) -> Optional[GroupId]:
    """Stable data key for group-scoped features.

    Message delivery must continue using the encrypted official
    ``group_openid``; this helper is for persisted score/settings keys that
    should survive a bot migration when an admin mapped the group to its
    former QQ number.
    """
    raw = get_event_group_id(event)
    mapped = resolve_group_legacy_id(raw)
    return mapped if mapped is not None else raw


def billing_group_id(event) -> Optional[int]:
    """Return the integer group key used by legacy BREAK/storage tables.

    Official QQ group identifiers are opaque strings.  An administrator's
    ``qgroupbind`` mapping takes precedence; otherwise derive a stable,
    SQLite-safe integer without ever coercing the encrypted openid directly.
    """
    raw = event_group_data_id(event)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        digest = hashlib.sha256(str(raw).encode('utf-8')).hexdigest()[:15]
        return int(digest, 16)


def is_group_message_event(event) -> bool:
    return get_event_group_id(event) is not None


def is_likely_qq_group_id(gid: GroupId) -> bool:
    """官方 QQ 群 openid 为非纯数字字符串。"""
    return isinstance(gid, str) and not gid.isdigit()


def get_sender_display_name(event) -> str:
    if is_qq_event(event):
        author = getattr(event, 'author', None)
        if author is not None:
            name = getattr(author, 'username', None) or getattr(author, 'nickname', None)
            if name:
                return str(name)
            if isinstance(author, dict):
                n = author.get('username') or author.get('nickname')
                if n:
                    return str(n)
    sender = getattr(event, 'sender', None)
    if sender is not None:
        card = getattr(sender, 'card', None) or ''
        nick = getattr(sender, 'nickname', None) or ''
        if card or nick:
            return str(card or nick)
        if isinstance(sender, dict):
            c = sender.get('card') or sender.get('nickname')
            if c:
                return str(c)
    return str(event.get_user_id())


def iter_message_segments(event) -> Iterable[Any]:
    msg = getattr(event, 'message', None)
    if msg is None:
        getter = getattr(event, 'get_message', None)
        if callable(getter):
            try:
                msg = getter()
            except Exception:
                msg = None
    if msg is None:
        return
    if isinstance(msg, str):
        yield MessageSegment.text(msg)
        return
    for seg in msg:
        yield seg


def parse_at_target_id(event) -> Optional[str]:
    for seg in iter_message_segments(event):
        seg_type = getattr(seg, 'type', None)
        data = getattr(seg, 'data', None) or {}
        if seg_type == 'at':
            qq = data.get('qq')
            if qq and str(qq) != 'all':
                return str(qq)
        elif seg_type == 'mention_user':
            uid = data.get('user_id')
            if uid:
                return str(uid)
    return None


def is_at_all_message(event) -> bool:
    for seg in iter_message_segments(event):
        seg_type = getattr(seg, 'type', None)
        data = getattr(seg, 'data', None) or {}
        if seg_type == 'at' and str(data.get('qq')) == 'all':
            return True
        if seg_type == 'mention_everyone':
            return True
    return False


def build_mention_message(target: UserId, text: str = '', *, event=None) -> Any:
    """按平台构建 @用户 + 可选正文。"""
    tid = str(target)
    if use_qq_mode(event):
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg

        parts: List[Any] = [QQSeg.mention_user(tid)]
        if text:
            parts.append(QQSeg.text(text))
        return QQMessage(parts)
    msg = MessageSegment.at(int(tid)) if tid.isdigit() else MessageSegment.at(tid)
    if text:
        return msg + MessageSegment.text(text)
    return msg


def resolve_reply_message(event=None, *, reply_message: bool = True) -> bool:
    """
    官方 QQ 被动回复由 adapter.send(event) 自动附带 msg_id；
    勿传 reply_message，避免触发不支持的引用 API。
    """
    if not reply_message:
        return False
    return not use_qq_mode(event)


def _ensure_qq_media_text(parts: List[Any]) -> List[Any]:
    """官方 QQ 发图/音频时 API 要求 content 非空，补一个空格文本段。"""
    if not parts:
        return parts
    from nonebot.adapters.qq.message import MessageSegment as QQSeg

    has_media = any(
        getattr(p, 'type', None) in ('file_image', 'file_audio', 'file_video', 'file_file')
        for p in parts
    )
    has_text = any(getattr(p, 'type', None) == 'text' for p in parts)
    if has_media and not has_text:
        return [QQSeg.text(' ')] + parts
    return parts


def resolve_event_bot(event):
    from nonebot import get_bot

    try:
        return get_bot(str(event.self_id))
    except Exception:
        return get_bot()


def format_forward_nodes_as_text(title: str, nodes: List[dict]) -> str:
    lines = [title]
    for node in nodes:
        data = node.get('data') or {}
        content = data.get('content')
        if content:
            lines.append(str(content))
    return '\n'.join(lines)


def rank_text_image(text: str) -> MessageSegment:
    """Render a ranking/leaderboard as one portable image message.

    OneBot forward nodes are not supported consistently by the official QQ
    adapter.  Keeping the renderer here lets old text-producing ranking
    services share the same image conversion and QQ attachment path.
    """
    from .image import image_to_base64, text_to_image

    # Keep a single unusually long title/name from producing an unreadably
    # wide image (song titles and nicknames can contain arbitrary user text).
    lines: list[str] = []
    for raw_line in str(text or '暂无数据。').splitlines() or ['暂无数据。']:
        line = str(raw_line)
        if not line:
            lines.append('')
            continue
        current: list[str] = []
        columns = 0
        for char in line:
            width = 2 if ord(char) > 0x7F else 1
            if current and columns + width > 42:
                lines.append(''.join(current))
                current = []
                columns = 0
            current.append(char)
            columns += width
        lines.append(''.join(current))
    rendered = '\n'.join(lines)
    return MessageSegment.image(image_to_base64(text_to_image(rendered)))


async def send_group_plain_text(bot, gid: GroupId, text: str) -> None:
    """向群发送纯文本（OneBot / 官方 QQ）。"""
    if is_likely_qq_group_id(gid):
        await bot.send_to_group(group_openid=str(gid), message=text)
    else:
        await bot.send_group_msg(group_id=int(gid), message=text)


def resolve_query_qqid(
    raw_id: Union[int, str],
    *,
    strict: bool = True,
    qq_mode: Optional[bool] = None,
) -> int:
    """
    查分水鱼/落雪用的 QQ 号。
    OneBot 下等于消息 user_id；官方 QQ 下读取 qbind 绑定的 legacy QQ。
    """
    if qq_mode is None:
        qq_mode = is_qq_official()
    if not qq_mode:
        return int(raw_id)
    pid = str(raw_id).strip()
    # ``@`` helpers may already have converted a target to the real QQ.  A
    # numeric value is therefore a valid legacy QQ even while official mode is
    # active; encrypted official openids are non-numeric strings.
    if pid.isdigit():
        return int(pid)
    bound = qq_bind_db.get_legacy_qq(pid)
    if bound is not None:
        return bound
    if strict:
        from .maimaidx_error import QBindRequiredError
        raise QBindRequiredError(pid)
    return int(raw_id) if str(raw_id).isdigit() else 0


def resolve_score_qqid(event, at_qq: Optional[int] = None) -> int:
    """成绩类指令：@ 他人时解析对方绑定 QQ，否则解析发送者。"""
    mode = use_qq_mode(event)
    if at_qq is not None:
        return resolve_query_qqid(at_qq, qq_mode=mode)
    if mode:
        return resolve_query_qqid(str(event.get_user_id()), qq_mode=True)
    return int(event.get_user_id())


def platform_user_id(event) -> str:
    """Bot 内部功能（BREAK、猜歌积分等）始终用平台 user id。"""
    return str(event.get_user_id())


def billing_user_id(event) -> int:
    """BREAK 扣费主体：官方 QQ 优先用 qbind 的 legacy QQ，否则 openid 稳定哈希。"""
    if use_qq_mode(event):
        pid = platform_user_id(event)
        bound = qq_bind_db.get_legacy_qq(pid)
        if bound is not None:
            return bound
        digest = hashlib.sha256(pid.encode()).hexdigest()[:15]
        return int(digest, 16)
    return int(event.get_user_id())


def _onebot_record_path(seg: MessageSegment) -> Optional[Path]:
    if seg.type != 'record':
        return None
    raw = seg.data.get('file') or seg.data.get('url') or ''
    if not raw:
        return None
    s = str(raw)
    if s.startswith('file://'):
        return Path(s[7:])
    p = Path(s)
    return p if p.is_file() else None


def _onebot_video_path(seg: MessageSegment) -> Optional[Path]:
    if seg.type != 'video':
        return None
    raw = seg.data.get('file') or seg.data.get('url') or ''
    if not raw:
        return None
    if isinstance(raw, Path):
        return raw.resolve() if raw.is_file() else None
    s = str(raw).strip()
    if s.startswith('file://'):
        # file:///abs/path 与 file://abs/path 都兼容
        from urllib.parse import unquote, urlparse

        parsed = urlparse(s)
        candidate = Path(unquote(parsed.path))
        if candidate.is_file():
            return candidate.resolve()
        # 少数实现把路径放在 netloc
        alt = Path(unquote(parsed.netloc + parsed.path))
        return alt.resolve() if alt.is_file() else None
    p = Path(s)
    return p.resolve() if p.is_file() else None


def local_video_segment(path: Union[str, Path]) -> MessageSegment:
    """本地视频段：统一绝对路径，供 OneBot / 官方 QQ 适配解析。"""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f'视频文件不存在: {p}')
    return MessageSegment.video(str(p))


def _onebot_image_bytes(seg: MessageSegment) -> Optional[bytes]:
    if seg.type != 'image':
        return None
    raw = seg.data.get('file') or seg.data.get('url') or ''
    if not raw:
        return None
    s = str(raw)
    if s.startswith('base64://'):
        return base64.b64decode(s[9:])
    if s.startswith('file://'):
        return Path(s[7:]).read_bytes()
    return None


def _iter_onebot_segments(result: Any) -> Iterable[MessageSegment]:
    if isinstance(result, MessageSegment):
        yield result
    elif isinstance(result, Message):
        yield from result


def adapt_reply_payload(result: Any, *, footer: str = '', event=None) -> Any:
    """
    将插件内 OneBot 消息段转为当前平台可发送的形态。
    官方 QQ 需 file_image(bytes)，不能发 base64:// 的 OneBot 图。
    """
    qq_mode = use_qq_mode(event)

    if isinstance(result, str):
        if not qq_mode:
            return result
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg

        parts: List[Any] = []
        if result.strip():
            parts.append(QQSeg.text(result))
        if footer:
            parts.append(QQSeg.text(footer))
        return QQMessage(parts) if parts else QQMessage([QQSeg.text('（无内容）')])

    if not qq_mode:
        if footer:
            return result + MessageSegment.text(footer)
        return result

    from nonebot.adapters.qq.message import Message as QQMessage
    from nonebot.adapters.qq.message import MessageSegment as QQSeg

    parts: List[Any] = []
    for seg in _iter_onebot_segments(result):
        if seg.type == 'image':
            data = _onebot_image_bytes(seg)
            if data:
                parts.append(QQSeg.file_image(data))
        elif seg.type == 'text':
            text = str(seg.data.get('text') or '')
            if text:
                parts.append(QQSeg.text(text))
    if footer:
        parts.append(QQSeg.text(footer))
    parts = _ensure_qq_media_text(parts)
    if not parts:
        return QQMessage([QQSeg.text('成绩图发送失败，请联系管理员。')])
    return QQMessage(parts)


def build_image_message(image: Union[bytes, BytesIO, str, Any], *, event=None) -> Any:
    """按平台与配置构建图片消息。"""
    if isinstance(image, BytesIO):
        image = image.getvalue()
    if use_qq_mode(event) and isinstance(image, bytes):
        from nonebot.adapters.qq.message import MessageSegment as QQSeg
        return QQSeg.file_image(image)
    if isinstance(image, bytes):
        b64 = 'base64://' + base64.b64encode(image).decode()
        return MessageSegment.image(b64)
    if isinstance(image, str) and image.startswith('base64://'):
        if use_qq_mode(event):
            from nonebot.adapters.qq.message import MessageSegment as QQSeg
            return QQSeg.file_image(base64.b64decode(image[9:]))
        return MessageSegment.image(image)
    if isinstance(image, MessageSegment):
        return image
    return MessageSegment.image(image)


async def finish_reply(matcher, payload: Any, *, reply: bool = True, event=None) -> None:
    """统一 finish：官方 QQ 自动转换消息段。"""
    await plugin_finish(matcher, payload, event=event, reply_message=reply)


async def finish_with_image(matcher, image_msg, *, footer: str = '', reply: bool = True, event=None) -> None:
    """统一 finish：可选 QQ 卡片形态（当前为图片 + 文本）。"""
    payload = adapt_reply_payload(image_msg, footer=footer, event=event)
    await plugin_finish(matcher, payload, event=event, reply_message=reply)


def adapt_guess_outbound(message: Any, *, event=None) -> Any:
    """
    猜歌出站消息：OneBot 图/音/视频/文 → 当前平台可发送形态。
    官方 QQ 将 image/record/video 转为 file_image/file_audio/file_video。
    """
    if not use_qq_mode(event):
        return message

    mod = type(message).__module__
    if 'adapters.qq' in mod:
        return message

    from nonebot.adapters.qq.message import Message as QQMessage
    from nonebot.adapters.qq.message import MessageSegment as QQSeg

    if isinstance(message, str):
        return QQMessage([QQSeg.text(message)]) if message else QQMessage([QQSeg.text(' ')])

    segments: List[MessageSegment] = []
    if isinstance(message, MessageSegment):
        segments = [message]
    elif isinstance(message, Message):
        segments = list(message)
    else:
        return message

    parts: List[Any] = []
    for seg in segments:
        if seg.type == 'text':
            text = str(seg.data.get('text') or '')
            if text:
                parts.append(QQSeg.text(text))
        elif seg.type == 'image':
            data = _onebot_image_bytes(seg)
            if data:
                parts.append(QQSeg.file_image(data))
        elif seg.type == 'record':
            audio_path = _onebot_record_path(seg)
            if audio_path:
                parts.append(QQSeg.file_audio(audio_path))
        elif seg.type == 'video':
            video_path = _onebot_video_path(seg)
            if video_path:
                parts.append(QQSeg.file_video(video_path))
            else:
                log.warning(
                    f'[maimai] 官方 QQ 视频段无法解析路径，已跳过: '
                    f'file={seg.data.get("file")!r}'
                )
        elif seg.type == 'at':
            qq = seg.data.get('qq')
            if str(qq) == 'all':
                parts.append(QQSeg.mention_everyone())
            elif qq:
                parts.append(QQSeg.mention_user(str(qq)))

    if not parts:
        return QQMessage([QQSeg.text('（消息发送失败）')])
    parts = _ensure_qq_media_text(parts)
    return QQMessage(parts)


async def plugin_send(matcher, message: Any, *, event=None, reply_message: bool = True) -> Any:
    reply = resolve_reply_message(event, reply_message=reply_message)
    payload = adapt_reply_payload(message, event=event)
    return await matcher.send(payload, reply_message=reply)


async def plugin_finish(
    matcher,
    message: Any = None,
    *,
    footer: str = '',
    event=None,
    reply_message: bool = True,
) -> None:
    reply = resolve_reply_message(event, reply_message=reply_message)
    if message is None:
        await matcher.finish(reply_message=reply)
        return
    await matcher.finish(
        adapt_reply_payload(message, footer=footer, event=event),
        reply_message=reply,
    )
