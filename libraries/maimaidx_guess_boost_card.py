"""猜歌限时加倍卡：群管理发放，下次猜对消耗一张并 ×2 积分。"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, Field

from ..config import guess_boost_card_file
from .maimaidx_platform import GroupId, UserId, user_data_id
from .tool import writefile

DEFAULT_CARD_HOURS = 24
MAX_CARDS_PER_GRANT = 10


class BoostCard(BaseModel):
    expires_at: float
    issued_at: float = 0
    issued_by: str = ''


class UserBoostCards(BaseModel):
    cards: List[BoostCard] = Field(default_factory=list)


class GroupBoostCards(BaseModel):
    members: Dict[str, UserBoostCards] = Field(default_factory=dict)


class BoostCardStore(BaseModel):
    groups: Dict[str, GroupBoostCards] = Field(default_factory=dict)


class GuessBoostCardManager:

    def __init__(self) -> None:
        if guess_boost_card_file.exists():
            with open(guess_boost_card_file, 'r', encoding='utf-8') as f:
                self.store = BoostCardStore.model_validate(json.load(f))
        else:
            self.store = BoostCardStore()

    @staticmethod
    def _gid_key(gid: GroupId) -> str:
        try:
            from .maimaidx_qq_bind import qq_bind_db

            mapped = qq_bind_db.get_group_legacy_id(str(gid))
            if mapped is not None:
                return str(mapped)
        except Exception:
            pass
        return str(gid)

    @staticmethod
    def _uid_key(uid: UserId) -> str:
        return str(user_data_id(uid))

    def _purge_expired(self, user: UserBoostCards, *, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        user.cards = [c for c in user.cards if c.expires_at > ts]

    def _get_user(self, gid: GroupId, uid: UserId) -> UserBoostCards:
        gk = self._gid_key(gid)
        uk = self._uid_key(uid)
        if gk not in self.store.groups:
            self.store.groups[gk] = GroupBoostCards()
        group = self.store.groups[gk]
        if uk not in group.members:
            group.members[uk] = UserBoostCards()
        user = group.members[uk]
        self._purge_expired(user)
        return user

    async def _save(self) -> None:
        await writefile(guess_boost_card_file, self.store.model_dump())

    def active_count(self, gid: GroupId, uid: UserId) -> int:
        return len(self._get_user(gid, uid).cards)

    def nearest_expiry_hours(self, gid: GroupId, uid: UserId) -> Optional[float]:
        user = self._get_user(gid, uid)
        if not user.cards:
            return None
        remain = min(c.expires_at for c in user.cards) - time.time()
        return max(0.0, remain / 3600)

    async def grant(
        self,
        gid: GroupId,
        uid: UserId,
        *,
        count: int = 1,
        hours: float = DEFAULT_CARD_HOURS,
        issuer_uid: UserId,
    ) -> Tuple[int, float]:
        count = max(1, min(int(count), MAX_CARDS_PER_GRANT))
        hours = max(1.0, float(hours))
        now = time.time()
        ttl = hours * 3600
        user = self._get_user(gid, uid)
        for _ in range(count):
            user.cards.append(BoostCard(
                expires_at=now + ttl,
                issued_at=now,
                issued_by=self._uid_key(issuer_uid),
            ))
        await self._save()
        return count, hours

    async def grant_many(
        self,
        gid: GroupId,
        uids: list[UserId],
        *,
        count: int = 1,
        hours: float = DEFAULT_CARD_HOURS,
        issuer_uid: UserId,
    ) -> tuple[int, float]:
        """向多人各发放若干张卡，仅持久化一次。"""
        count = max(1, min(int(count), MAX_CARDS_PER_GRANT))
        hours = max(1.0, float(hours))
        now = time.time()
        ttl = hours * 3600
        issuer = self._uid_key(issuer_uid)
        for uid in uids:
            user = self._get_user(gid, uid)
            for _ in range(count):
                user.cards.append(BoostCard(
                    expires_at=now + ttl,
                    issued_at=now,
                    issued_by=issuer,
                ))
        await self._save()
        return len(uids), hours

    async def consume_one(self, gid: GroupId, uid: UserId) -> bool:
        user = self._get_user(gid, uid)
        if not user.cards:
            return False
        user.cards.sort(key=lambda c: c.expires_at)
        user.cards.pop(0)
        await self._save()
        return True


guess_boost_card = GuessBoostCardManager()
