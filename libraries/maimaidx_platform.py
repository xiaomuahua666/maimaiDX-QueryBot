"""平台适配：OneBot / 官方 QQ，查分 QQ 解析与消息形态。"""

from __future__ import annotations

import base64
import hashlib
import re
from html import escape as html_escape
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, List, Optional, Union

from nonebot.adapters.onebot.v11 import Message, MessageSegment

GroupId = Union[int, str]
UserId = Union[int, str]

from ..config import log, maiconfig
from .maimaidx_qq_bind import qq_bind_db


def _segment_user_id(seg: Any) -> str:
    data = getattr(seg, 'data', None) or {}
    return str(
        data.get('user_id')
        or data.get('member_openid')
        or data.get('id')
        or ''
    ).strip()


def _event_mention_is_bot(event: Any, seg: Any) -> bool:
    """Use the raw event mention list when the segment lost its flags."""
    uid = _segment_user_id(seg)
    if not uid or event is None:
        return False
    for mention in getattr(event, 'mentions', None) or ():
        mention_ids = {
            str(value).strip()
            for value in (
                getattr(mention, 'id', None),
                getattr(mention, 'user_id', None),
                getattr(mention, 'member_openid', None),
            )
            if value not in (None, '')
        }
        if uid not in mention_ids:
            continue
        if (
            getattr(mention, 'is_you', None) is True
            or getattr(mention, 'is_bot', None) is True
            or getattr(mention, 'bot', None) is True
        ):
            return True
    return False


_QQ_AT_MARKUP_RE = re.compile(
    r'<qqbot-at-user\s+id="(?P<id>[^"]+)"\s*/\s*>'
)


def _expand_qq_at_markup(event: Any, message: Any) -> Any:
    """Parse modern QQ @ markup when the installed adapter regex misses it.

    Adapter 1.7.x only accepts ``\\w+`` ids and therefore leaves a modern
    ``<qqbot-at-user ... />`` token as plain text for some valid openids.  The
    gateway's ``mentions`` list is authoritative, so only expand ids present
    there; ordinary user text containing a similar token stays untouched.
    """
    if message is None or not hasattr(message, '__iter__'):
        return message
    try:
        from nonebot.adapters.qq.message import MessageSegment as QQSeg
    except ImportError:
        return message

    mention_info: dict[str, tuple[str, bool]] = {}
    for mention in getattr(event, 'mentions', None) or ():
        ids = {
            str(value).strip()
            for value in (
                getattr(mention, 'id', None),
                getattr(mention, 'user_id', None),
                getattr(mention, 'member_openid', None),
            )
            if value not in (None, '')
        }
        if not ids:
            continue
        username = str(
            getattr(mention, 'username', None)
            or getattr(mention, 'nickname', None)
            or ''
        ).strip()
        is_bot = bool(
            getattr(mention, 'is_you', None) is True
            or getattr(mention, 'is_bot', None) is True
            or getattr(mention, 'bot', None) is True
        )
        for mention_id in ids:
            mention_info[mention_id] = (username, is_bot)

    if not mention_info:
        return message

    expanded: list[Any] = []
    changed = False
    for segment in message:
        if getattr(segment, 'type', None) != 'text':
            expanded.append(segment)
            continue
        data = getattr(segment, 'data', None) or {}
        text = str(data.get('text') or '')
        cursor = 0
        segment_changed = False
        for match in _QQ_AT_MARKUP_RE.finditer(text):
            uid = match.group('id').strip()
            info = mention_info.get(uid)
            if info is None:
                continue
            if match.start() > cursor:
                expanded.append(QQSeg.text(text[cursor:match.start()]))
            at_segment = QQSeg.mention_user(uid, username=info[0] or None)
            at_segment.data['is_bot'] = info[1]
            expanded.append(at_segment)
            cursor = match.end()
            segment_changed = True
            changed = True
        if segment_changed:
            if cursor < len(text):
                expanded.append(QQSeg.text(text[cursor:]))
        else:
            expanded.append(segment)

    if not changed:
        return message
    try:
        return message.__class__(expanded)
    except Exception:
        return message


def _qq_bot_mention(
    seg: Any,
    event=None,
    *,
    leading: bool = False,
    allow_event_fallback: bool = False,
) -> bool:
    """Return whether a QQ message segment is the bot's own mention.

    ``nonebot-adapter-qq`` annotates the mention with ``is_bot`` when the
    gateway includes the member list.  A few gateway versions omit that bit,
    but an ``AT_MESSAGE_CREATE``/``GROUP_AT_MESSAGE_CREATE`` event is still
    intrinsically addressed to the bot, so its first mention is the bot.
    """
    if getattr(seg, 'type', None) != 'mention_user':
        return False
    data = getattr(seg, 'data', None) or {}
    if data.get('is_bot') is True or data.get('is_you') is True:
        return True
    if _event_mention_is_bot(event, seg):
        return True
    # A few fake/older event implementations expose only ``is_tome``.  This
    # fallback is deliberately opt-in: parse_at_target_id must never discard a
    # real target merely because the event is a group-at event.
    if leading and allow_event_fallback and event is not None:
        try:
            return bool(event.is_tome())
        except Exception:
            return bool(getattr(event, 'to_me', False))
    return False


def _normalize_qq_inbound_message(
    event,
    message,
    *,
    allow_event_fallback: bool = False,
):
    """Make official QQ group mentions transparent to NoneBot's rule engine.

    The QQ adapter represents ``@bot command`` as
    ``[mention_user(bot), text(" command")]``.  NoneBot's command trie and
    several text rules expect the first segment to be text, while command
    handlers still need all subsequent user mentions for ``@target``
    arguments.  Work on a copy so the original gateway payload is untouched.
    """
    if message is None or not hasattr(message, 'copy'):
        return message
    try:
        normalized = message.copy()
    except Exception:
        return message
    normalized = _expand_qq_at_markup(event, normalized)

    removed_bot_mention = False
    while normalized:
        # The current QQ wire format may leave a whitespace-only text segment
        # before ``<qqbot-at-user ... />``.  Only discard that padding when a
        # confirmed bot mention follows, so ordinary leading user text and a
        # real first @target remain untouched.
        mention_index = 0
        while mention_index < len(normalized):
            candidate = normalized[mention_index]
            if getattr(candidate, 'type', None) != 'text':
                break
            candidate_data = getattr(candidate, 'data', None) or {}
            if str(candidate_data.get('text') or '').strip():
                break
            mention_index += 1
        if mention_index >= len(normalized):
            break
        first = normalized[mention_index]
        if not _qq_bot_mention(
            first,
            event,
            leading=not removed_bot_mention,
            allow_event_fallback=allow_event_fallback,
        ):
            break
        for _ in range(mention_index + 1):
            normalized.pop(0)
        removed_bot_mention = True

    # The gateway includes a padding space before the command after an @.
    # Strip it only when we removed the bot mention; ordinary user text keeps
    # its original leading whitespace.
    if removed_bot_mention and normalized:
        first = normalized[0]
        if getattr(first, 'type', None) == 'text':
            data = getattr(first, 'data', None) or {}
            data['text'] = str(data.get('text') or '').lstrip()
            if not data['text']:
                normalized.pop(0)

    return normalized


class _QQMessageRuleProxy:
    """Minimal event view used while running NoneBot's command trie."""

    __slots__ = ('_event', '_message')

    def __init__(self, event, message):
        self._event = event
        self._message = message

    def get_type(self):
        return self._event.get_type()

    def get_message(self):
        return self._message


def _qq_event_self_id(event: Any) -> str:
    """Return the active QQ Bot id for legacy handlers using event.self_id."""
    try:
        from nonebot.matcher import current_bot

        bot = current_bot.get(None)
        if bot is not None and getattr(bot, 'self_id', None) not in (None, ''):
            return str(bot.self_id)
    except Exception:
        pass
    for mention in getattr(event, 'mentions', None) or ():
        if (
            getattr(mention, 'is_you', None) is True
            or getattr(mention, 'is_bot', None) is True
            or getattr(mention, 'bot', None) is True
        ):
            value = (
                getattr(mention, 'id', None)
                or getattr(mention, 'member_openid', None)
            )
            if value not in (None, ''):
                return str(value)
    return ''


def install_qq_event_compat() -> None:
    """Expose the OneBot-style aliases used by older command modules.

    ``nonebot-adapter-qq`` deliberately exposes ``id``/``author`` while many
    mature plugin commands still read ``message_id``/``user_id``/``sender``.
    Adding read-only aliases keeps both adapters usable without changing the
    encrypted values or pretending they are real QQ numbers.
    """
    try:
        from nonebot.adapters.qq.event import (
            GroupMessageCreateEvent,
            QQMessageEvent,
        )
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
        'self_id': property(_qq_event_self_id),
        'sender': property(lambda event: getattr(event, 'author', None)),
    }
    for name, value in aliases.items():
        if not hasattr(QQMessageEvent, name):
            try:
                setattr(QQMessageEvent, name, value)
            except Exception:
                log.debug(f'[platform] 无法安装官方 QQ 事件兼容属性 {name}')

    # ``GroupAtMessageCreateEvent.get_message`` puts the bot's own mention
    # before the command text.  Normalize both the base QQ event and the group
    # override so command/regex/startswith rules see the actual command while
    # later @target segments remain available to plugin handlers.
    try:
        for event_cls in (QQMessageEvent, GroupMessageCreateEvent):
            original_get_message = event_cls.get_message
            if getattr(original_get_message, '_maimaidx_qq_inbound_normalized', False):
                continue

            def _normalized_get_message(event, _original=original_get_message):
                had_cached_message = hasattr(event, 'message')
                message = _original(event)
                normalized = _normalize_qq_inbound_message(
                    event,
                    message,
                    # The adapter's first raw payload may omit segment flags;
                    # once a message is cached, an unmarked leading mention is
                    # more likely to be a real @target and must be preserved.
                    allow_event_fallback=not had_cached_message,
                )
                if normalized is not message:
                    try:
                        event.message = normalized
                    except Exception:
                        pass
                return normalized

            _normalized_get_message._maimaidx_qq_inbound_normalized = True
            event_cls.get_message = _normalized_get_message

        original_get_plaintext = QQMessageEvent.get_plaintext
        if not getattr(original_get_plaintext, '_maimaidx_qq_inbound_normalized', False):
            def _normalized_get_plaintext(event):
                # get_plaintext is used by regex/fullmatch/keyword rules.  The
                # normalized get_message already removes the gateway padding;
                # lstrip is a final guard for adapter payloads that bypass it.
                return str(original_get_plaintext(event) or '').lstrip()

            _normalized_get_plaintext._maimaidx_qq_inbound_normalized = True
            QQMessageEvent.get_plaintext = _normalized_get_plaintext
    except Exception as exc:
        log.debug(f'[platform] 官方 QQ 入站消息规范化补丁安装失败: {exc}')

    # NoneBot computes the command prefix once before checking matchers.  Keep
    # a fallback at that boundary as well: custom/fake QQ event subclasses may
    # override get_message after the class patch above, and should still be
    # able to dispatch ``@bot 锐评一下``.
    try:
        from nonebot.rule import TrieRule

        original_trie_get_value = TrieRule.get_value
        if not getattr(original_trie_get_value, '_maimaidx_qq_inbound_normalized', False):
            def _qq_aware_trie_get_value(cls, bot, event, state):
                if is_qq_event(event):
                    try:
                        raw_message = event.get_message()
                        normalized = _normalize_qq_inbound_message(
                            event,
                            raw_message,
                            allow_event_fallback=not hasattr(event, 'message'),
                        )
                        if normalized is not raw_message:
                            return original_trie_get_value(
                                bot, _QQMessageRuleProxy(event, normalized), state
                            )
                    except Exception:
                        # Preserve NoneBot's normal parser error handling.
                        pass
                return original_trie_get_value(bot, event, state)

            _qq_aware_trie_get_value._maimaidx_qq_inbound_normalized = True
            TrieRule.get_value = classmethod(_qq_aware_trie_get_value)
    except Exception as exc:
        log.debug(f'[platform] 官方 QQ 命令解析兼容补丁安装失败: {exc}')

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
        import nonebot.dependencies as dependencies
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
            # ``nonebot.internal.params`` and ``nonebot.dependencies`` both keep
            # module-level references imported from dependency_utils.  Patch all
            # of them; Dependent._solve_field uses the dependencies package copy
            # and would otherwise TypeMisMatch-skip every CommandArg handler.
            internal_params.check_field_type = _check_field_type
            dependencies.check_field_type = _check_field_type
    except Exception as exc:
        log.warning(f'[platform] 官方 QQ 依赖注入兼容补丁安装失败: {exc}')

    try:
        from nonebot.matcher import Matcher, current_event

        original_matcher_send = Matcher.send
        if not getattr(original_matcher_send, '_maimaidx_qq_compat', False):
            async def _qq_aware_matcher_send(cls, message, **kwargs):
                event = current_event.get(None)
                if is_qq_event(event):
                    message = ensure_sender_mention(message, event)
                    if _is_onebot_payload(message):
                        message = adapt_guess_outbound(message, event=event)
                    # ``reply_message`` is a OneBot-only matcher kwarg.  The QQ
                    # adapter already performs a passive reply with msg_id.
                    kwargs['reply_message'] = False
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
                extra_reply_count = 0
                if is_qq_event(event):
                    message = ensure_sender_mention(message, event)
                    if _is_onebot_payload(message):
                        message = adapt_guess_outbound(message, event=event)
                    # Keep a caption with one local media when the official
                    # endpoint accepts the combined payload.  The adapter
                    # still needs a split for multiple media objects because
                    # its wire format has only one ``media`` slot.  The QQ
                    # adapter increments _reply_seq once per Bot.send call;
                    # account for every follow-up media wire message here as well.
                    split = _split_qq_media_message(message)
                    extra_reply_count = len(split[1]) if split else 0
                result = await original_qq_send(self, event, message, **kwargs)
                if extra_reply_count:
                    try:
                        event._reply_seq += extra_reply_count
                    except Exception:
                        pass
                return result

            _qq_aware_bot_send._maimaidx_qq_compat = True
            QQBot.send = _qq_aware_bot_send

        # ``send_to_group``/``send_to_c2c`` are also used by scheduled and
        # active-message paths, which bypass ``Bot.send``.  Keep the split at
        # the adapter boundary so those paths cannot leak media-adjacent text.
        original_qq_send_to_group = getattr(QQBot, 'send_to_group', None)
        if (
            original_qq_send_to_group is not None
            and not getattr(original_qq_send_to_group, '_maimaidx_qq_media_split', False)
        ):
            async def _qq_aware_send_to_group(
                self,
                group_openid,
                message,
                msg_id=None,
                msg_seq=None,
                event_id=None,
                msg_ref_id=None,
            ):
                split = _split_qq_media_message(message)
                if split is None:
                    return await original_qq_send_to_group(
                        self,
                        group_openid=group_openid,
                        message=message,
                        msg_id=msg_id,
                        msg_seq=msg_seq,
                        event_id=event_id,
                        msg_ref_id=msg_ref_id,
                    )
                text_message, media_messages = split
                await original_qq_send_to_group(
                    self,
                    group_openid=group_openid,
                    message=text_message,
                    msg_id=msg_id,
                    msg_seq=msg_seq,
                    event_id=event_id,
                    msg_ref_id=msg_ref_id,
                )
                result = None
                for index, media_message in enumerate(media_messages, 1):
                    media_seq = msg_seq
                    media_event_id = event_id
                    if msg_id is not None:
                        media_seq = (int(msg_seq) if msg_seq is not None else 0) + index
                    elif event_id is not None:
                        # An event id is single-use; follow-up media is an
                        # ordinary active message after the text response.
                        media_event_id = None
                    result = await original_qq_send_to_group(
                        self,
                        group_openid=group_openid,
                        message=media_message,
                        msg_id=msg_id,
                        msg_seq=media_seq,
                        event_id=media_event_id,
                        msg_ref_id=None,
                    )
                return result

            _qq_aware_send_to_group._maimaidx_qq_media_split = True
            QQBot.send_to_group = _qq_aware_send_to_group

        original_qq_send_to_c2c = getattr(QQBot, 'send_to_c2c', None)
        if (
            original_qq_send_to_c2c is not None
            and not getattr(original_qq_send_to_c2c, '_maimaidx_qq_media_split', False)
        ):
            async def _qq_aware_send_to_c2c(
                self,
                openid,
                message,
                msg_id=None,
                msg_seq=None,
                event_id=None,
                msg_ref_id=None,
            ):
                split = _split_qq_media_message(message)
                if split is None:
                    return await original_qq_send_to_c2c(
                        self,
                        openid=openid,
                        message=message,
                        msg_id=msg_id,
                        msg_seq=msg_seq,
                        event_id=event_id,
                        msg_ref_id=msg_ref_id,
                    )
                text_message, media_messages = split
                await original_qq_send_to_c2c(
                    self,
                    openid=openid,
                    message=text_message,
                    msg_id=msg_id,
                    msg_seq=msg_seq,
                    event_id=event_id,
                    msg_ref_id=msg_ref_id,
                )
                result = None
                for index, media_message in enumerate(media_messages, 1):
                    media_seq = msg_seq
                    media_event_id = event_id
                    if msg_id is not None:
                        media_seq = (int(msg_seq) if msg_seq is not None else 0) + index
                    elif event_id is not None:
                        media_event_id = None
                    result = await original_qq_send_to_c2c(
                        self,
                        openid=openid,
                        message=media_message,
                        msg_id=msg_id,
                        msg_seq=media_seq,
                        event_id=media_event_id,
                        msg_ref_id=None,
                    )
                return result

            _qq_aware_send_to_c2c._maimaidx_qq_media_split = True
            QQBot.send_to_c2c = _qq_aware_send_to_c2c

        original_qq_call_api = QQBot.call_api
        if not getattr(original_qq_call_api, '_maimaidx_qq_compat', False):
            async def _qq_aware_call_api(self, api: str, **data):
                """Translate the small OneBot API subset used by this plugin."""
                api_name = str(api)
                if api_name in ('send_group_msg', 'send_private_msg'):
                    message = data.get('message', '')
                    if api_name == 'send_group_msg':
                        return await self.send_to_group(
                            group_openid=str(
                                resolve_group_delivery_id(data.get('group_id', ''))
                            ),
                            message=adapt_guess_outbound(message, force_qq=True),
                        )
                    return await self.send_to_c2c(
                        openid=str(
                            resolve_private_delivery_id(data.get('user_id', ''))
                        ),
                        message=adapt_guess_outbound(message, force_qq=True),
                    )
                if api_name in ('send_group_forward_msg', 'send_private_forward_msg'):
                    nodes = data.get('messages') or data.get('nodes') or []
                    # Official QQ has no OneBot forward-message equivalent.
                    # Keep this fallback as native Markdown so emoji and links
                    # are rendered by QQ instead of being rasterized by PIL.
                    payload = build_qq_forward_message(nodes, title='合并转发')
                    if api_name == 'send_group_forward_msg':
                        return await self.send_to_group(
                            group_openid=str(
                                resolve_group_delivery_id(data.get('group_id', ''))
                            ),
                            message=adapt_guess_outbound(payload, force_qq=True),
                        )
                    return await self.send_to_c2c(
                        openid=str(
                            resolve_private_delivery_id(data.get('user_id', ''))
                        ),
                        message=adapt_guess_outbound(payload, force_qq=True),
                    )
                if api_name == 'delete_msg':
                    message_id = str(data.get('message_id', '') or '')
                    group_id = (
                        data.get('group_id')
                        or data.get('group_openid')
                    )
                    user_id = data.get('user_id') or data.get('openid')
                    if not group_id and not user_id:
                        try:
                            from nonebot.matcher import current_event

                            event = current_event.get(None)
                        except Exception:
                            event = None
                        if event is not None:
                            group_id = getattr(event, 'group_openid', None) or getattr(
                                event, 'group_id', None
                            )
                            if not group_id:
                                user_id = platform_user_id(event)
                    if group_id:
                        return await self.delete_group_message(
                            group_openid=str(resolve_group_delivery_id(group_id)),
                            message_id=message_id,
                        )
                    if user_id:
                        return await self.delete_c2c_message(
                            openid=str(resolve_private_delivery_id(user_id)),
                            message_id=message_id,
                        )
                    raise RuntimeError(
                        '官方 QQ 撤回需要 group_openid 或 openid（仅 message_id 不够）'
                    )
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
                    group_openid=str(resolve_group_delivery_id(group_id)),
                    message=adapt_guess_outbound(message, force_qq=True),
                )
            QQBot.send_group_msg = _send_group_msg
        if not hasattr(QQBot, 'send_private_msg'):
            async def _send_private_msg(self, *, user_id, message, **kwargs):
                return await self.send_to_c2c(
                    openid=str(resolve_private_delivery_id(user_id)),
                    message=adapt_guess_outbound(message, force_qq=True),
                )
            QQBot.send_private_msg = _send_private_msg
        if not hasattr(QQBot, 'delete_msg'):
            async def _delete_msg(self, *, message_id, **kwargs):
                return await self.call_api('delete_msg', message_id=message_id, **kwargs)
            QQBot.delete_msg = _delete_msg
    except (ImportError, AttributeError):
        pass

    # Official QQ handlers often raise QBindRequiredError before any reply.
    # Without this safety net the matcher dies silently and users see "no response".
    try:
        from nonebot.exception import (
            FinishedException,
            IgnoredException,
            PausedException,
            RejectedException,
            SkippedException,
            StopPropagation,
        )
        from nonebot.matcher import Matcher

        from .maimaidx_error import (
            BreakInsufficientError,
            QBindRequiredError,
            format_command_error,
        )

        _passthrough = (
            FinishedException,
            IgnoredException,
            PausedException,
            RejectedException,
            SkippedException,
            StopPropagation,
        )
        original_simple_run = Matcher.simple_run
        if not getattr(original_simple_run, '_maimaidx_user_error_reply', False):
            async def _simple_run_with_user_errors(
                self, bot, event, state, stack=None, dependency_cache=None
            ):
                try:
                    return await original_simple_run(
                        self, bot, event, state, stack, dependency_cache
                    )
                except _passthrough:
                    raise
                except (QBindRequiredError, BreakInsufficientError) as exc:
                    module = ' '.join(
                        str(x)
                        for x in (
                            getattr(self, 'module_name', None),
                            getattr(self, 'plugin_name', None),
                            getattr(self, 'module', None),
                        )
                        if x
                    ).lower()
                    if 'maimaidx' not in module:
                        raise
                    # original_simple_run's ensure_context() already reset
                    # current_bot before this except runs; restore it so send works.
                    try:
                        with self.ensure_context(bot, event):
                            await bot.send(
                                event,
                                adapt_reply_payload(
                                    format_command_error(exc), event=event
                                ),
                            )
                    except Exception as send_exc:
                        log.warning(
                            f'[platform] 业务异常回复失败: '
                            f'{type(send_exc).__name__}: {send_exc}'
                        )
                        raise exc from send_exc
                    raise FinishedException

            _simple_run_with_user_errors._maimaidx_user_error_reply = True
            Matcher.simple_run = _simple_run_with_user_errors
    except Exception as exc:
        log.warning(f'[platform] 业务异常回复补丁安装失败: {exc}')

    # Adapter 1.7 may receive Dispatch.data as a bare string for some payloads.
    try:
        from nonebot.adapters.qq.adapter import Adapter as QQAdapter

        original_payload_to_event = QQAdapter.payload_to_event
        if not getattr(original_payload_to_event, '_maimaidx_qq_compat', False):
            def _payload_to_event_safe(payload):
                data = getattr(payload, 'data', None)
                if isinstance(data, dict):
                    return original_payload_to_event(payload)

                # RESUMED / unknown frames may send data as "" or a bare string.
                class _PayloadProxy:
                    __slots__ = ('_payload',)

                    def __init__(self, raw):
                        self._payload = raw

                    @property
                    def id(self):
                        return self._payload.id

                    @property
                    def type(self):
                        return self._payload.type

                    @property
                    def data(self):
                        return {}

                return original_payload_to_event(_PayloadProxy(payload))

            _payload_to_event_safe._maimaidx_qq_compat = True
            QQAdapter.payload_to_event = staticmethod(_payload_to_event_safe)
    except Exception as exc:
        log.warning(f'[platform] QQ payload_to_event 兼容补丁安装失败: {exc}')


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


def is_qq_bot(bot: Any) -> bool:
    """判断 Bot 实例是否来自官方 QQ 适配器。"""
    if bot is None:
        return False
    modules = {
        type(bot).__module__,
        type(getattr(bot, 'adapter', None)).__module__,
    }
    return any(
        module.startswith('nonebot.adapters.qq') or '.adapters.qq' in module
        for module in modules
    )


def _bot_registry_values(bots: Optional[dict] = None) -> list[Any]:
    if bots is None:
        try:
            from nonebot import get_bots

            bots = get_bots()
        except Exception:
            bots = {}
    return list((bots or {}).values())


def resolve_group_bot(group_id: GroupId, bots: Optional[dict] = None):
    """按群投递地址选择正确的适配器 Bot。

    Persisted records may still contain the old numeric QQ group.  Once a
    qgroupbind mapping resolves it to an openid, prefer the official QQ Bot;
    an unmapped numeric id remains an ordinary OneBot target.
    """
    candidates = _bot_registry_values(bots)
    if not candidates:
        return None
    delivery_id = resolve_group_delivery_id(group_id)
    want_qq = is_likely_qq_group_id(delivery_id)
    for candidate in candidates:
        if is_qq_bot(candidate) is want_qq:
            return candidate
    return candidates[0]


def resolve_private_delivery_id(user_id: UserId) -> UserId:
    """Resolve a legacy numeric QQ user to an official QQ openid if bound."""
    raw = str(user_id).strip()
    if not raw.isdigit():
        return raw
    try:
        mapped = qq_bind_db.get_platform_id(int(raw))
    except Exception:
        mapped = None
    return mapped or int(raw)


def resolve_private_bot(user_id: UserId, bots: Optional[dict] = None):
    """Choose a Bot for a private target, honoring qbind when available."""
    candidates = _bot_registry_values(bots)
    if not candidates:
        return None
    delivery_id = resolve_private_delivery_id(user_id)
    want_qq = not str(delivery_id).isdigit()
    for candidate in candidates:
        if is_qq_bot(candidate) is want_qq:
            return candidate
    return candidates[0]


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


def resolve_group_delivery_id(group_id: GroupId) -> GroupId:
    """Resolve a persisted old group key to its current official QQ address."""
    if is_likely_qq_group_id(group_id):
        return group_id
    try:
        mapped = qq_bind_db.get_platform_group_id(int(group_id))
    except (AttributeError, TypeError, ValueError):
        mapped = None
    return mapped or group_id


def user_data_id(user_id: UserId) -> UserId:
    """Stable user key for data created before the official QQ migration."""
    raw = str(user_id).strip()
    if raw.isdigit():
        return int(raw)
    try:
        mapped = qq_bind_db.get_legacy_qq(raw)
    except Exception:
        mapped = None
    return mapped if mapped is not None else raw


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
    for index, seg in enumerate(iter_message_segments(event)):
        seg_type = getattr(seg, 'type', None)
        data = getattr(seg, 'data', None) or {}
        if seg_type == 'at':
            qq = data.get('qq')
            if qq and str(qq) != 'all':
                return str(qq)
        elif seg_type == 'mention_user':
            # Official QQ group events may retain the leading @bot segment in
            # a cached/fake message even after command normalization.  It is
            # addressing the command receiver, never the requested target.
            if _qq_bot_mention(seg, event, leading=index == 0):
                continue
            # Adapter releases have used ``user_id``, ``member_openid`` and
            # ``id`` for the same opaque QQ identity.  Keep target parsing
            # independent of that wire-format detail.
            uid = _segment_user_id(seg)
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
    username = None
    if event is not None:
        author = getattr(event, 'author', None) or getattr(event, 'sender', None)
        if author is not None and str(
            getattr(author, 'member_openid', None)
            or getattr(author, 'user_openid', None)
            or getattr(author, 'user_id', None)
            or getattr(author, 'id', '')
            or ''
        ) == tid:
            username = (
                getattr(author, 'username', None)
                or getattr(author, 'nickname', None)
                or getattr(author, 'card', None)
            )
    if use_qq_mode(event):
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg

        parts: List[Any] = [_qq_mention_segment(tid, username=username)]
        if text:
            parts.append(QQSeg.text(text))
        return QQMessage(parts)
    msg = MessageSegment.at(int(tid)) if tid.isdigit() else MessageSegment.at(tid)
    if text:
        return msg + MessageSegment.text(text)
    return msg


def _message_starts_with_mention(message: Any) -> bool:
    if message is None or isinstance(message, str):
        return False
    if isinstance(message, MessageSegment):
        return message.type in ('at', 'mention_user')
    try:
        first = next(iter(message), None)
    except TypeError:
        first = None
    if first is None:
        return False
    seg_type = getattr(first, 'type', None)
    return seg_type in ('at', 'mention_user')


_QQ_MENTION_SEGMENT_CLASS = None


def qq_at_markup(user_id: UserId) -> str:
    """Return Tencent's current clickable @ markup for message content."""
    return f'<qqbot-at-user id="{html_escape(str(user_id).strip(), quote=True)}" />'


def _qq_mention_segment(target: UserId, *, username: Optional[str] = None) -> Any:
    """Build a real official-QQ mention, resolving old numeric QQ ids when possible."""
    from nonebot.adapters.qq.message import MessageSegment as QQSeg

    tid = str(target).strip()
    if tid.isdigit():
        platform_id = qq_bind_db.get_platform_id(int(tid))
        if platform_id:
            tid = platform_id
        else:
            return QQSeg.text(f'@{username or tid}')
    if not tid:
        return QQSeg.text(f'@{username or "你"}')

    # Keep a typed mention segment for duplicate-prefix detection while using
    # Tencent's current content markup.  The adapter still serializes the
    # deprecated ``<@openid>`` form as of 1.7.1.
    global _QQ_MENTION_SEGMENT_CLASS
    from nonebot.adapters.qq.message import MentionUser

    if _QQ_MENTION_SEGMENT_CLASS is None:
        class _QQBotAtUser(MentionUser):
            def __str__(self) -> str:
                return qq_at_markup(self.data['user_id'])

        _QQBotAtUser.__module__ = MentionUser.__module__
        _QQ_MENTION_SEGMENT_CLASS = _QQBotAtUser
    data = {'user_id': tid}
    if username:
        data['username'] = username
    return _QQ_MENTION_SEGMENT_CLASS('mention_user', data)


def _prepend_qq_markdown_mention(message: Any, event: Any) -> Any | None:
    """Put a sender @ inside Markdown, where QQ parses it as a blue label.

    A standalone ``mention_user`` segment becomes the ``content`` field while
    a Markdown message is sent through ``markdown.content``.  Mixing the two
    fields makes the former render as plain text, so Markdown messages need
    the official ``<qqbot-at-user id="..." />`` token embedded directly in
    their content.
    """
    module = type(message).__module__
    if not module.startswith('nonebot.adapters.qq'):
        return None
    try:
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg
    except ImportError:
        return None
    if isinstance(message, QQSeg):
        segments = [message]
    elif isinstance(message, QQMessage):
        segments = list(message)
    else:
        return None
    markdown_segments = [seg for seg in segments if getattr(seg, 'type', None) == 'markdown']
    if not markdown_segments:
        return None
    uid = platform_user_id(event)
    if not uid:
        return message
    markup = qq_at_markup(uid)
    output: list[Any] = []
    for segment in segments:
        if getattr(segment, 'type', None) != 'markdown':
            output.append(segment)
            continue
        model = (getattr(segment, 'data', None) or {}).get('markdown')
        content = str(getattr(model, 'content', None) or '')
        if markup not in content:
            content = f'{markup}\n{content.lstrip()}'
        output.append(QQSeg.markdown(content))
    try:
        return QQMessage(output)
    except Exception:
        return message


def ensure_sender_mention(message: Any, event) -> Any:
    """群聊回复前缀 @发送者，便于用户确认请求已被处理。"""
    if event is None or message is None:
        return message
    if get_event_group_id(event) is None:
        return message
    if _message_starts_with_mention(message):
        return message

    uid = platform_user_id(event)
    author = getattr(event, 'author', None) or getattr(event, 'sender', None)
    nickname = ''
    if author is not None:
        nickname = str(
            getattr(author, 'username', None)
            or getattr(author, 'nickname', None)
            or getattr(author, 'card', None)
            or ''
        ).strip()

    if isinstance(message, str):
        body = message.lstrip('\n')
        if use_qq_mode(event):
            from nonebot.adapters.qq.message import Message as QQMessage
            from nonebot.adapters.qq.message import MessageSegment as QQSeg
            prefix = _qq_mention_segment(uid, username=nickname or None)
            if body:
                return QQMessage([prefix, QQSeg.text('\n'), QQSeg.text(body)])
            return QQMessage([prefix])
        return build_mention_message(uid, f'\n{body}' if body else '', event=event)

    if use_qq_mode(event):
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg
        markdown_message = _prepend_qq_markdown_mention(message, event)
        if markdown_message is not None:
            return markdown_message
        prefix = _qq_mention_segment(uid, username=nickname or None)
        if 'adapters.qq' in type(message).__module__:
            if isinstance(message, QQSeg):
                return QQMessage([prefix, QQSeg.text('\n'), message])
            return QQMessage([prefix, QQSeg.text('\n')] + list(message))
        # Convert OneBot segments before prepending the mention.  Serializing
        # an image segment with ``str(message)`` leaks its ``base64://`` value
        # as plain text; official QQ then renders the huge payload as a
        # collapsed "... / click to view full message" bubble instead of an
        # image.  ``adapt_guess_outbound`` preserves image/audio/video media.
        converted = adapt_guess_outbound(message, event=event)
        if 'adapters.qq' in type(converted).__module__:
            if isinstance(converted, QQSeg):
                converted_parts = [converted]
            else:
                converted_parts = list(converted)
            return QQMessage([prefix, QQSeg.text('\n')] + converted_parts)
        return QQMessage([prefix, QQSeg.text('\n'), QQSeg.text(str(message))])

    prefix = MessageSegment.at(int(uid) if str(uid).isdigit() else uid) + MessageSegment.text(
        '\n'
    )
    if isinstance(message, MessageSegment):
        return prefix + message
    if isinstance(message, Message):
        return prefix + message
    return prefix + MessageSegment.text(str(message))


def resolve_reply_message(event=None, *, reply_message: bool = True) -> bool:
    """
    官方 QQ 被动回复由 adapter.send(event) 自动附带 msg_id；
    勿传 reply_message，避免触发不支持的引用 API。
    """
    if not reply_message:
        return False
    return not use_qq_mode(event)


_QQ_MEDIA_SEGMENT_TYPES = frozenset(
    {
        'image', 'audio', 'video', 'file',
        'file_image', 'file_audio', 'file_video', 'file_file',
    }
)


def _ensure_qq_media_text(parts: List[Any]) -> List[Any]:
    """官方 QQ 发图/音频时 API 要求 content 非空，补一个空格文本段。"""
    if not parts:
        return parts
    from nonebot.adapters.qq.message import MessageSegment as QQSeg

    has_media = any(
        getattr(p, 'type', None) in _QQ_MEDIA_SEGMENT_TYPES
        for p in parts
    )
    has_text = any(getattr(p, 'type', None) == 'text' for p in parts)
    if has_media and not has_text:
        return [QQSeg.text(' ')] + parts
    return parts


def _split_qq_media_message(message: Any) -> Optional[tuple[Any, list[Any]]]:
    """Build API-safe QQ wire messages without dropping media captions.

    The official QQ API uses ``msg_type=7`` for media and exposes one media
    object per request. It displays ordinary caption text, but does not parse
    interactive @ markup there. Move mentions into a preceding text request
    while keeping the ordinary caption beside the first image. Extra media
    objects each become a follow-up request instead of being discarded by the
    adapter's ``[-1]`` extraction.
    """
    module = type(message).__module__
    if not module.startswith('nonebot.adapters.qq'):
        return None
    try:
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg
    except ImportError:
        return None

    if isinstance(message, QQSeg):
        segments = [message]
    elif isinstance(message, QQMessage):
        segments = list(message)
    else:
        try:
            segments = list(message)
        except TypeError:
            return None

    media_types = _QQ_MEDIA_SEGMENT_TYPES
    text_types = {'text', 'mention_user', 'mention_everyone', 'mention_channel', 'emoji'}
    media = [seg for seg in segments if getattr(seg, 'type', None) in media_types]
    if not media:
        return None

    mention_types = {'mention_user', 'mention_everyone', 'mention_channel'}
    mentions = [
        seg for seg in segments if getattr(seg, 'type', None) in mention_types
    ]
    if mentions:
        caption = [
            seg
            for seg in segments
            if getattr(seg, 'type', None) not in media_types | mention_types
        ]
        # ensure_sender_mention inserts one newline between the @ prefix and
        # body.  Once the prefix is its own message that separator must not
        # become a blank line above the media caption.
        if caption and getattr(caption[0], 'type', None) == 'text':
            first_text = str((getattr(caption[0], 'data', None) or {}).get('text') or '')
            first_text = first_text.lstrip('\n')
            caption = caption[1:]
            if first_text:
                caption.insert(0, QQSeg.text(first_text))
        first_media = _ensure_qq_media_text([*caption, media[0]])
        return (
            QQMessage(mentions),
            [QQMessage(first_media)]
            + [QQMessage([QQSeg.text(' '), segment]) for segment in media[1:]],
        )

    if len(media) == 1:
        return None
    text = [seg for seg in segments if getattr(seg, 'type', None) in text_types]
    first_parts = [*text, media[0]]
    if not text:
        first_parts.insert(0, QQSeg.text(' '))
    return (
        QQMessage(first_parts),
        [QQMessage([QQSeg.text(' '), segment]) for segment in media[1:]],
    )


def resolve_event_bot(event):
    from nonebot import get_bot

    try:
        return get_bot(str(event.self_id))
    except Exception:
        return get_bot()


def format_forward_nodes_as_text(title: str, nodes: List[dict]) -> str:
    return flatten_forward_nodes(nodes, title=title)


_FORWARD_URL_RE = re.compile(r"https?://[^\s<>\]）】》]+")


def _qq_markdownize_forward_line(line: str) -> str:
    """Turn URLs in a flattened forward line into clickable Markdown."""
    match = _FORWARD_URL_RE.search(line)
    if not match:
        return line
    # Preserve links that a node already supplied in Markdown form.
    if match.start() >= 2 and line[match.start() - 2 : match.start()] == "](":
        return line
    url = match.group(0).rstrip('.,!?;:')
    trailing = match.group(0)[len(url):] + line[match.end():]
    prefix = line[:match.start()]
    # Normal preview nodes use ``绿谱 https://...``.  Use the node label as
    # link text; bare URLs get a short generic label.
    label = prefix.strip()
    if label:
        before = prefix[: len(prefix) - len(prefix.lstrip())]
        return f"{before}[{label}]({url}){trailing}"
    return f"[打开链接]({url}){trailing}"


def qq_forward_markdown(nodes: List[dict], *, title: str = '') -> str:
    """Flatten forward nodes into native official-QQ Markdown content."""
    text = flatten_forward_nodes(nodes, title=title)
    if not text.strip():
        return ''
    return '\n'.join(_qq_markdownize_forward_line(line) for line in text.splitlines())


def build_qq_forward_message(nodes: List[dict], *, title: str = '') -> Any:
    """Build a native QQ Markdown message from OneBot forward nodes."""
    from nonebot.adapters.qq.message import Message as QQMessage
    from nonebot.adapters.qq.message import MessageSegment as QQSeg

    content = qq_forward_markdown(nodes, title=title)
    return QQMessage([QQSeg.markdown(content or '（无内容）')])


def flatten_forward_nodes(nodes: List[dict], *, title: str = '') -> str:
    """把 OneBot 合并转发（含嵌套 node）压成可读纯文本。

    官方 QQ 下会在此基础上构建原生 Markdown；文本过长/过多会导致
    客户端消息加载失败，因此这里做截断保护。
    """
    lines: list[str] = []
    if title:
        lines.append(title)

    # Safety limits for QQ client rendering.
    max_lines = 80
    max_total_chars = 6000
    truncated = False
    total_chars = sum(len(x) for x in lines)

    def walk(node_list: Iterable[dict], section: str = '') -> None:
        nonlocal truncated, total_chars
        for node in node_list:
            if truncated:
                return
            data = node.get('data') or {}
            nickname = str(data.get('nickname') or data.get('name') or '').strip()
            content = data.get('content')
            if isinstance(content, list):
                header = nickname or section
                if header:
                    if len(lines) < max_lines:
                        lines.append(f'━━ {header} ━━')
                        total_chars += len(lines[-1])
                    else:
                        truncated = True
                        lines.append('…（已截断）')
                        return
                walk(content, header)
                continue
            if content in (None, ''):
                continue
            text = str(content).strip()
            if not text:
                continue
            if text.startswith('[CQ:image') or text.startswith('base64://'):
                if len(lines) < max_lines:
                    lines.append('（附图）')
                    total_chars += len(lines[-1])
                else:
                    truncated = True
                    lines.append('…（已截断）')
                    return
                continue
            if len(lines) >= max_lines:
                truncated = True
                lines.append('…（已截断）')
                return
            if total_chars + len(text) > max_total_chars:
                truncated = True
                lines.append('…（已截断）')
                return
            lines.append(text)
            total_chars += len(text)

    walk(nodes)
    return '\n'.join(lines)


async def deliver_forward_messages(
    bot,
    event,
    nodes: List[dict],
    *,
    title: str = '',
    reply_message: bool = False,
) -> None:
    """OneBot 发合并转发；官方 QQ 用原生 Markdown 保留 Emoji 和可复制内容。

    Official QQ does not expose OneBot's nested forward-node API.  Sending
    the flattened result as native Markdown keeps Unicode/Emoji intact, makes
    preview URLs clickable, and avoids turning user-facing text into a PIL
    image. Long output is split on line boundaries to stay below the platform
    text limit.
    """
    if not nodes:
        return
    if use_qq_mode(event):
        text = qq_forward_markdown(nodes, title=title)
        if not text.strip():
            return
        from nonebot.adapters.qq.message import Message as QQMessage
        from nonebot.adapters.qq.message import MessageSegment as QQSeg

        # Keep each native text payload comfortably below QQ's group-message
        # limit while preserving complete lines (and therefore Emoji pairs).
        max_chars = 3500
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            if current and current_len + len(line) > max_chars:
                chunks.append(''.join(current).rstrip('\n'))
                current = []
                current_len = 0
            if len(line) > max_chars:
                if current:
                    chunks.append(''.join(current).rstrip('\n'))
                    current = []
                    current_len = 0
                for start in range(0, len(line), max_chars):
                    part = line[start:start + max_chars]
                    if part:
                        chunks.append(part.rstrip('\n'))
                continue
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append(''.join(current).rstrip('\n'))
        if not chunks:
            chunks = [text]
        for chunk in chunks:
            if chunk:
                await bot.send(event, QQMessage([QQSeg.markdown(chunk)]))
        return
    if get_event_group_id(event) is not None:
        await bot.call_api(
            'send_group_forward_msg',
            group_id=get_event_group_id(event),
            messages=nodes,
        )
    else:
        await bot.call_api(
            'send_private_forward_msg',
            user_id=event.get_user_id(),
            messages=nodes,
        )


def foreign_recall_notice(event) -> str:
    """用户消息撤回失败时的提示；官方 QQ 本身不支持撤回用户消息。"""
    return '⚠️ Bot 无法撤回该消息，请立即手动撤回。'


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
    if is_qq_bot(bot) or is_likely_qq_group_id(resolve_group_delivery_id(gid)):
        await bot.send_to_group(
            group_openid=str(resolve_group_delivery_id(gid)), message=text
        )
        return
    await bot.send_group_msg(group_id=int(gid), message=text)


async def send_group_message(bot, gid: GroupId, message: Any) -> None:
    """向指定群发送任意消息，并在官方 QQ 下保留媒体和 @ 段。"""
    if is_qq_bot(bot) or is_likely_qq_group_id(resolve_group_delivery_id(gid)):
        await bot.send_to_group(
            group_openid=str(resolve_group_delivery_id(gid)),
            message=adapt_guess_outbound(message, force_qq=True),
        )
        return
    await bot.send_group_msg(group_id=int(gid), message=message)


async def send_private_message(bot, uid: UserId, message: Any) -> None:
    """向私聊目标发送消息，兼容旧 QQ 号与官方 QQ openid。"""
    delivery_id = resolve_private_delivery_id(uid)
    if is_qq_bot(bot) or not str(delivery_id).isdigit():
        if not hasattr(bot, 'send_to_c2c'):
            raise RuntimeError('官方 QQ Bot 不支持 C2C 发送')
        await bot.send_to_c2c(
            openid=str(delivery_id),
            message=adapt_guess_outbound(message, force_qq=True),
        )
        return
    await bot.send_private_msg(user_id=int(delivery_id), message=message)


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


def require_account_qqid(event, at_qq: Optional[int] = None) -> int:
    """账号类指令统一身份：官方 QQ 必须先 qbind，否则抛出可回复的提示。"""
    return resolve_score_qqid(event, at_qq)


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

    # 已是官方 QQ 消息段：直接发送，避免再被当成 OneBot 丢掉 @。
    result_module = type(result).__module__
    if result_module.startswith('nonebot.adapters.qq'):
        if footer:
            from nonebot.adapters.qq.message import Message as QQMessage
            from nonebot.adapters.qq.message import MessageSegment as QQSeg

            if isinstance(result, QQSeg):
                return QQMessage([result, QQSeg.text(footer)])
            return QQMessage(list(result) + [QQSeg.text(footer)])
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
        elif seg.type == 'at':
            qq = seg.data.get('qq')
            if str(qq) == 'all':
                parts.append(QQSeg.mention_everyone())
            elif qq not in (None, ''):
                parts.append(_qq_mention_segment(qq))
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


def build_markdown_link_message(
    title: str,
    links: Iterable[tuple[str, str]],
    *,
    event=None,
) -> Any:
    """Build a clickable link message on adapters that support Markdown.

    Official QQ renders text embedded in a converted forward/image as pixels,
    so URLs in that fallback cannot be opened. Keep the link presentation at
    the platform boundary: QQ gets custom Markdown plus URL buttons, while
    OneBot callers receive ordinary text and continue using their own message
    protocol.
    """
    normalized = [
        (str(label).strip(), str(url).strip())
        for label, url in links
        if str(label).strip() and str(url).strip()
    ]
    if not normalized:
        return MessageSegment.text(str(title or ''))
    if not use_qq_mode(event):
        body = '\n'.join(f'{label}: {url}' for label, url in normalized)
        return MessageSegment.text(
            f'{title}\n{body}' if str(title or '').strip() else body
        )

    from nonebot.adapters.qq.message import Message as QQMessage
    from nonebot.adapters.qq.message import MessageSegment as QQSeg
    from nonebot.adapters.qq.models import (
        Action,
        Button,
        InlineKeyboard,
        InlineKeyboardRow,
        MessageKeyboard,
        RenderData,
    )

    heading = str(title or '').strip()
    content_lines = [heading] if heading else []
    content_lines.extend(f'[{label}]({url})' for label, url in normalized)
    buttons = [
        Button(
            id=f'maimaidx-link-{index}',
            render_data=RenderData(label=label, style=1),
            action=Action(type=0, data=url),
        )
        for index, (label, url) in enumerate(normalized, 1)
    ]
    rows = [
        InlineKeyboardRow(buttons=buttons[start : start + 5])
        for start in range(0, len(buttons), 5)
    ]
    keyboard = MessageKeyboard(content=InlineKeyboard(rows=rows))
    return QQMessage(
        [
            QQSeg.markdown('\n'.join(content_lines)),
            QQSeg.keyboard(keyboard),
        ]
    )


async def finish_reply(matcher, payload: Any, *, reply: bool = True, event=None) -> None:
    """统一 finish：官方 QQ 自动转换消息段。"""
    await plugin_finish(matcher, payload, event=event, reply_message=reply)


async def finish_with_image(matcher, image_msg, *, footer: str = '', reply: bool = True, event=None) -> None:
    """统一 finish：可选 QQ 卡片形态（当前为图片 + 文本）。"""
    payload = adapt_reply_payload(image_msg, footer=footer, event=event)
    await plugin_finish(matcher, payload, event=event, reply_message=reply)


def adapt_guess_outbound(
    message: Any,
    *,
    event=None,
    force_qq: bool = False,
) -> Any:
    """
    猜歌出站消息：OneBot 图/音/视频/文 → 当前平台可发送形态。
    官方 QQ 将 image/record/video 转为 file_image/file_audio/file_video。
    """
    if not (force_qq or use_qq_mode(event)):
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
                parts.append(_qq_mention_segment(qq))

    if not parts:
        return QQMessage([QQSeg.text('（消息发送失败）')])
    parts = _ensure_qq_media_text(parts)
    return QQMessage(parts)


async def plugin_send(
    matcher,
    message: Any,
    *,
    event=None,
    reply_message: bool = True,
    mention_sender: Optional[bool] = None,
) -> Any:
    if mention_sender is None:
        mention_sender = event is not None and get_event_group_id(event) is not None
    if mention_sender:
        message = ensure_sender_mention(message, event)
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
    mention_sender: Optional[bool] = None,
) -> None:
    if mention_sender is None:
        mention_sender = (
            message is not None
            and event is not None
            and get_event_group_id(event) is not None
        )
    if message is not None and mention_sender:
        message = ensure_sender_mention(message, event)
    reply = resolve_reply_message(event, reply_message=reply_message)
    if message is None:
        await matcher.finish(reply_message=reply)
        return
    await matcher.finish(
        adapt_reply_payload(message, footer=footer, event=event),
        reply_message=reply,
    )


def extract_sent_message_id(result: Any) -> str:
    """Normalize OneBot / 官方 QQ 发送回执里的 message id。"""
    if result is None:
        return ''
    if isinstance(result, dict):
        mid = result.get('message_id') or result.get('id')
        return str(mid).strip() if mid not in (None, '') else ''
    mid = getattr(result, 'message_id', None)
    if mid in (None, ''):
        mid = getattr(result, 'id', None)
    return str(mid).strip() if mid not in (None, '') else ''


async def recall_message(
    bot,
    event,
    *,
    message_id: Optional[Union[int, str]] = None,
    timeout_seconds: float = 3.0,
    foreign: bool = False,
) -> bool:
    """撤回一条消息；官方 QQ 只能撤回机器人自己发的消息（2 分钟内）。"""
    import asyncio
    import time as _time

    mid = str(
        message_id
        if message_id not in (None, '')
        else getattr(event, 'message_id', None) or getattr(event, 'id', '') or ''
    ).strip()
    if not mid:
        return False

    started_at = _time.perf_counter()
    try:
        kwargs: dict[str, Any] = {'message_id': mid}
        if is_qq_event(event):
            group_id = getattr(event, 'group_openid', None) or getattr(event, 'group_id', None)
            if group_id not in (None, ''):
                kwargs['group_id'] = str(group_id)
            else:
                kwargs['user_id'] = platform_user_id(event)
        else:
            group_id = getattr(event, 'group_id', None)
            if group_id not in (None, ''):
                kwargs['group_id'] = group_id
            else:
                user_id = getattr(event, 'user_id', None)
                if user_id not in (None, ''):
                    kwargs['user_id'] = user_id

        await asyncio.wait_for(
            bot.delete_msg(**kwargs),
            timeout=max(0.5, float(timeout_seconds)),
        )
    except Exception as exc:
        log.warning(
            f'[Recall] 撤回失败 mid={mid}：{type(exc).__name__} '
            f'({_time.perf_counter() - started_at:.2f}s)'
        )
        return False
    log.info(
        f'[Recall] 已撤回 mid={mid} ({_time.perf_counter() - started_at:.2f}s)'
    )
    return True
