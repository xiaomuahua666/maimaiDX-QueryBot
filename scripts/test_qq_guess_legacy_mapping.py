"""Official QQ bindings must continue using legacy guess group/user keys."""

import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries.maimaidx_guess_boost_card import (
    BoostCard,
    BoostCardStore,
    GroupBoostCards,
    GuessBoostCardManager,
    UserBoostCards,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_guess_score import (
    GuessGroupScores,
    GuessMemberScore,
    GuessScoreEvent,
    GuessScoreEventGroup,
    GuessScoreEventStore,
    GuessScoreManager,
    GuessScoreStore,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_guess_scheduler import (
    _broadcast_group_ids,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_model import (
    AliasesPush,
    FeatureSwitch,
    GuessSwitch,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_music import (
    FeatureManager,
    GroupAlias,
    Guess,
    guess as global_guess,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_platform import (
    resolve_group_delivery_id,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_guess_sync import GuessSyncManager
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_bind import QqBindDatabase


GROUP_OPENID = "official-group-openid"
USER_OPENID = "official-user-openid"
LEGACY_GROUP = 993795066
LEGACY_QQ = 123456789

original_group = QqBindDatabase.get_group_legacy_id
original_group_delivery = QqBindDatabase.get_platform_group_id
original_user = QqBindDatabase.get_legacy_qq
original_guess_enable = list(global_guess.switch.enable)
QqBindDatabase.get_group_legacy_id = (
    lambda self, gid: LEGACY_GROUP if str(gid) == GROUP_OPENID else None
)
QqBindDatabase.get_legacy_qq = (
    lambda self, uid: LEGACY_QQ if str(uid) == USER_OPENID else None
)
QqBindDatabase.get_platform_group_id = (
    lambda self, gid: GROUP_OPENID if int(gid) == LEGACY_GROUP else None
)

try:
    scores = object.__new__(GuessScoreManager)
    scores.store = GuessScoreStore(
        groups={
            str(LEGACY_GROUP): GuessGroupScores(
                members={
                    str(LEGACY_QQ): GuessMemberScore(score=42, name="旧账号玩家")
                }
            )
        }
    )
    scores.event_store = GuessScoreEventStore(
        groups={
            str(LEGACY_GROUP): GuessScoreEventGroup(
                events=[
                    GuessScoreEvent(
                        uid=str(LEGACY_QQ),
                        name="旧账号玩家",
                        mode=GuessScoreManager.MODE_SONG,
                        points=7,
                        at="2026-08-04 12:00:00",
                    )
                ]
            )
        }
    )

    stats = scores.build_user_guess_stats(GROUP_OPENID, USER_OPENID)
    assert stats["total_score"] == 42
    assert stats["name"] == "旧账号玩家"
    assert stats["modes"][GuessScoreManager.MODE_SONG]["count"] == 1
    assert scores.get_member_or_none(GROUP_OPENID, USER_OPENID) is not None

    guess = object.__new__(Guess)
    guess.switch = GuessSwitch(enable=[LEGACY_GROUP], disable=[])
    assert guess.is_enabled(GROUP_OPENID)
    guess.switch = GuessSwitch(enable=[GROUP_OPENID], disable=[])
    assert guess.is_enabled(GROUP_OPENID)
    assert resolve_group_delivery_id(LEGACY_GROUP) == GROUP_OPENID
    assert resolve_group_delivery_id(GROUP_OPENID) == GROUP_OPENID

    sync = object.__new__(GuessSyncManager)
    assert sync.is_play_group(GROUP_OPENID)
    assert sync.peer_group(GROUP_OPENID) == 1072033605

    global_guess.switch.enable = [LEGACY_GROUP, GROUP_OPENID]
    assert _broadcast_group_ids() == [GROUP_OPENID]

    aliases = object.__new__(GroupAlias)
    aliases.push = AliasesPush(enable=[LEGACY_GROUP], disable=[])
    assert not aliases.is_disabled(GROUP_OPENID)
    aliases.push.disable = [LEGACY_GROUP]
    assert aliases.is_disabled(GROUP_OPENID)

    features = object.__new__(FeatureManager)
    features.switch = FeatureSwitch()
    features.switch.score.disable = [LEGACY_GROUP]
    assert not features.is_enabled(GROUP_OPENID, "score")
    features.switch.score.disable = [GROUP_OPENID]
    assert not features.is_enabled(GROUP_OPENID, "score")

    cards = object.__new__(GuessBoostCardManager)
    cards.store = BoostCardStore(
        groups={
            str(LEGACY_GROUP): GroupBoostCards(
                members={
                    str(LEGACY_QQ): UserBoostCards(
                        cards=[BoostCard(expires_at=time.time() + 3600)]
                    )
                }
            )
        }
    )
    assert cards.active_count(GROUP_OPENID, USER_OPENID) == 1
finally:
    global_guess.switch.enable = original_guess_enable
    QqBindDatabase.get_group_legacy_id = original_group
    QqBindDatabase.get_platform_group_id = original_group_delivery
    QqBindDatabase.get_legacy_qq = original_user

print("qq guess legacy mapping tests: ok")
