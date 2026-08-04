"""主群与猜歌群之间的猜歌数据同步 / 冲突确认。

- 1072033605：主群（引导去猜歌群）
- 993795066：猜歌群（正式游玩）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from loguru import logger as log

from ..config import guess_sync_prefs_file
from .maimaidx_platform import GroupId, UserId, resolve_group_legacy_id
from .tool import writefile

MAIN_GUESS_GROUP_ID = 1072033605
PLAY_GUESS_GROUP_ID = 993795066
SYNC_GROUP_IDS = frozenset({MAIN_GUESS_GROUP_ID, PLAY_GUESS_GROUP_ID})

MAIN_GROUP_REDIRECT = (
    '为了保证用户使用体验，如需游玩猜歌请添加群聊 993795066'
)

CONFLICT_PROMPT = (
    '感谢游玩AWMC猜歌，检测到您在 主群 拥有游戏数据，是否覆盖本群游戏数据？\n'
    '是 - 覆盖\n'
    '否 - 不覆盖（保存主群备份且不再询问）\n'
    '请回复「是」或「否」（发送「取消」可退出）。'
)

CONFIRM_YES_PROMPT = (
    '二次确认：确定用主群数据覆盖本群猜歌数据吗？\n'
    '回复「确认」执行覆盖，回复「取消」放弃。'
)

CONFIRM_NO_PROMPT = (
    '二次确认：确定不覆盖本群数据，并保存主群备份且以后不再询问吗？\n'
    '回复「确认」执行，回复「取消」放弃。'
)

PENDING_TTL_SECONDS = 10 * 60


@dataclass
class PendingSync:
    gid: GroupId
    uid: str
    stage: str  # choose | confirm_yes | confirm_no | confirm_import
    created_at: float = field(default_factory=time.time)


class GuessSyncManager:
    def __init__(self, prefs_path: Optional[Path] = None) -> None:
        self.prefs_path = Path(prefs_path or guess_sync_prefs_file)
        self._pending: Dict[Tuple[str, str], PendingSync] = {}
        self._prefs: Dict[str, Any] = {'users': {}}
        self._load_prefs()

    def _load_prefs(self) -> None:
        try:
            if self.prefs_path.exists():
                data = json.loads(self.prefs_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    users = data.get('users')
                    if not isinstance(users, dict):
                        users = {}
                    self._prefs = {'users': users}
                    return
        except Exception as exc:
            log.warning(f'[GuessSync] 读取偏好失败: {exc}')
        self._prefs = {'users': {}}

    async def _save_prefs(self) -> None:
        self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
        await writefile(self.prefs_path, self._prefs)

    def _user_pref(self, uid: UserId) -> Dict[str, Any]:
        uk = str(uid)
        users = self._prefs.setdefault('users', {})
        cur = users.get(uk)
        if not isinstance(cur, dict):
            cur = {}
            users[uk] = cur
        return cur

    def is_dismissed(self, uid: UserId) -> bool:
        return bool(self._user_pref(uid).get('dismiss_overwrite_prompt'))

    def peer_group(self, gid: GroupId) -> Optional[int]:
        try:
            mapped = resolve_group_legacy_id(gid)
            g = int(mapped) if mapped is not None else int(gid)
        except (TypeError, ValueError):
            return None
        if g == MAIN_GUESS_GROUP_ID:
            return PLAY_GUESS_GROUP_ID
        if g == PLAY_GUESS_GROUP_ID:
            return MAIN_GUESS_GROUP_ID
        return None

    def is_main_group(self, gid: GroupId) -> bool:
        try:
            mapped = resolve_group_legacy_id(gid)
            return int(mapped) == MAIN_GUESS_GROUP_ID if mapped is not None else int(gid) == MAIN_GUESS_GROUP_ID
        except (TypeError, ValueError):
            return False

    def is_play_group(self, gid: GroupId) -> bool:
        try:
            mapped = resolve_group_legacy_id(gid)
            return int(mapped) == PLAY_GUESS_GROUP_ID if mapped is not None else int(gid) == PLAY_GUESS_GROUP_ID
        except (TypeError, ValueError):
            return False

    def get_pending(self, gid: GroupId, uid: UserId) -> Optional[PendingSync]:
        key = (str(gid), str(uid))
        item = self._pending.get(key)
        if not item:
            return None
        if time.time() - item.created_at > PENDING_TTL_SECONDS:
            self._pending.pop(key, None)
            return None
        return item

    def clear_pending(self, gid: GroupId, uid: UserId) -> None:
        self._pending.pop((str(gid), str(uid)), None)

    def set_pending(self, gid: GroupId, uid: UserId, stage: str) -> PendingSync:
        item = PendingSync(gid=gid, uid=str(uid), stage=stage)
        self._pending[(str(item.gid), item.uid)] = item
        return item

    async def _backup_main(self, uid: UserId) -> None:
        from .maimaidx_guess_score import guess_score

        member = guess_score.get_member_or_none(MAIN_GUESS_GROUP_ID, uid)
        events = [e.model_dump() for e in guess_score.list_user_events(MAIN_GUESS_GROUP_ID, uid)]
        pref = self._user_pref(uid)
        pref['main_backup'] = {
            'saved_at': datetime.now().isoformat(timespec='seconds'),
            'member': member.model_dump() if member else None,
            'events': events,
        }
        pref['dismiss_overwrite_prompt'] = True
        await self._save_prefs()

    async def apply_overwrite_from_main(self, uid: UserId) -> bool:
        from .maimaidx_guess_score import guess_score

        ok = await guess_score.copy_user_guess_data(
            MAIN_GUESS_GROUP_ID, PLAY_GUESS_GROUP_ID, uid
        )
        pref = self._user_pref(uid)
        pref['dismiss_overwrite_prompt'] = False
        pref['last_resolved_at'] = datetime.now().isoformat(timespec='seconds')
        pref['last_action'] = 'overwrite_from_main'
        await self._save_prefs()
        return ok

    async def apply_keep_local(self, uid: UserId) -> None:
        await self._backup_main(uid)
        pref = self._user_pref(uid)
        pref['last_resolved_at'] = datetime.now().isoformat(timespec='seconds')
        pref['last_action'] = 'keep_local_backup_main'
        await self._save_prefs()

    async def prepare_group_entry(
        self, gid: GroupId, uid: UserId
    ) -> Tuple[str, Optional[str]]:
        """开局入口：仅主群导流，不做任何自动同步/覆盖。"""
        if self.is_main_group(gid):
            return 'redirect', MAIN_GROUP_REDIRECT
        return 'ok', None

    async def start_manual_migrate(
        self, gid: GroupId, uid: UserId
    ) -> Tuple[str, Optional[str]]:
        """
        用户主动发送「迁移数据」。
        返回 (status, message)：
          - done：已完成（一致 / 单向导入），直接回复
          - need_prompt / pending：进入确认流程
          - reject：群不对等
        不会在开字母/猜歌得分时自动调用，避免异常覆盖。
        """
        if self.is_main_group(gid):
            return (
                'reject',
                MAIN_GROUP_REDIRECT + '\n如需迁移猜歌数据，请到猜歌群发送「迁移数据」。',
            )
        if not self.is_play_group(gid):
            return 'reject', '猜歌数据迁移仅支持在猜歌群 993795066 使用。'

        if self.get_pending(gid, uid):
            p = self.get_pending(gid, uid)
            assert p is not None
            if p.stage == 'choose':
                return 'pending', CONFLICT_PROMPT + '\n（请先完成本次选择）'
            if p.stage == 'confirm_yes':
                return 'pending', CONFIRM_YES_PROMPT + '\n（请先完成二次确认）'
            return 'pending', CONFIRM_NO_PROMPT + '\n（请先完成二次确认）'

        from .maimaidx_guess_score import guess_score

        main_has = guess_score.user_has_guess_data(MAIN_GUESS_GROUP_ID, uid)
        play_has = guess_score.user_has_guess_data(PLAY_GUESS_GROUP_ID, uid)

        if not main_has and not play_has:
            return 'done', '主群与本群都还没有猜歌数据，无需迁移。'

        if main_has and not play_has:
            # 单向导入也不静默：走二次确认（是=导入）
            self.set_pending(gid, uid, 'confirm_import')
            return (
                'need_prompt',
                '检测到主群有猜歌数据，本群为空。\n'
                '是否将主群数据导入本群？\n'
                '回复「确认」导入，回复「取消」放弃。',
            )

        if play_has and not main_has:
            return (
                'done',
                '本群已有猜歌数据，主群暂无对应数据。\n'
                '为避免误覆盖，不会自动改写主群；若只需在本群游玩可直接继续。',
            )

        # 两侧都有
        if guess_score.user_data_fingerprint(
            MAIN_GUESS_GROUP_ID, uid
        ) == guess_score.user_data_fingerprint(PLAY_GUESS_GROUP_ID, uid):
            return 'done', '主群与本群猜歌数据已一致，无需迁移。'

        # 手动迁移时即使曾选「不再询问」也允许再次确认
        self.set_pending(gid, uid, 'choose')
        return 'need_prompt', CONFLICT_PROMPT

    async def handle_reply(
        self, gid: GroupId, uid: UserId, text: str
    ) -> Tuple[bool, str]:
        """
        处理冲突确认回复。
        返回 (handled, reply_text)；handled=False 表示不是本流程消息。
        """
        pending = self.get_pending(gid, uid)
        if not pending:
            return False, ''

        raw = (text or '').strip()
        low = raw.lower()
        if low in {'取消', 'cancel', 'q', '退出'}:
            self.clear_pending(gid, uid)
            return True, '已取消猜歌数据同步确认。'

        if pending.stage == 'confirm_import':
            if raw not in {'确认', '确定'}:
                return True, (
                    '请回复「确认」将主群数据导入本群，或「取消」放弃。'
                )
            self.clear_pending(gid, uid)
            ok = await self.apply_overwrite_from_main(uid)
            if ok:
                return True, '已将主群猜歌数据导入本群。可以继续游玩啦~'
            return True, '导入失败：主群似乎没有可导入的猜歌数据。'

        if pending.stage == 'choose':
            if raw in {'是', '覆盖', 'yes', 'y', 'Y'}:
                self.set_pending(gid, uid, 'confirm_yes')
                return True, CONFIRM_YES_PROMPT
            if raw in {'否', '不覆盖', 'no', 'n', 'N'}:
                self.set_pending(gid, uid, 'confirm_no')
                return True, CONFIRM_NO_PROMPT
            return True, '请回复「是」或「否」；发送「取消」可退出。\n' + CONFLICT_PROMPT

        if pending.stage in {'confirm_yes', 'confirm_no'}:
            if raw not in {'确认', '确定'}:
                tip = (
                    CONFIRM_YES_PROMPT
                    if pending.stage == 'confirm_yes'
                    else CONFIRM_NO_PROMPT
                )
                return True, '请回复「确认」执行，或「取消」放弃。\n' + tip

            self.clear_pending(gid, uid)
            if pending.stage == 'confirm_yes':
                ok = await self.apply_overwrite_from_main(uid)
                if ok:
                    return True, '已用主群数据覆盖本群猜歌数据。可以继续游玩啦~'
                return True, '覆盖失败：主群似乎没有可导入的猜歌数据。'
            await self.apply_keep_local(uid)
            return True, (
                '已保留本群数据，并保存主群备份；以后不再询问是否覆盖。'
                '\n若以后仍要迁移，可主动发送「迁移数据」。'
            )

        self.clear_pending(gid, uid)
        return True, '同步会话已失效，请重新发送「迁移数据」。'


guess_sync = GuessSyncManager()
