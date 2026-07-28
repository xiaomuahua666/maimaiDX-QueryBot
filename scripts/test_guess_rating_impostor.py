#!/usr/bin/env python3
"""猜 Rating 分级/题主隔离与 B50 找内鬼逻辑回归。"""

from __future__ import annotations

import random
import sys
import types
from pathlib import Path

import nonebot


ROOT = Path(__file__).resolve().parents[1]
nonebot.init(maimaidxpath=str(ROOT), nickname={"test"})
package = types.ModuleType("nonebot_plugin_maimaidx")
package.__path__ = [str(ROOT)]
sys.modules["nonebot_plugin_maimaidx"] = package

from nonebot_plugin_maimaidx.libraries.maimaidx_guess_impostor import (  # noqa: E402
    GuessImpostorManager,
    build_impostor_cards,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_guess_rating import (  # noqa: E402
    GuessRatingManager,
    RATING_DIFFICULTIES,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_guess_score import GuessScoreManager  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_guess_stats_draw import (  # noqa: E402
    draw_personal_guess_stats,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_model import ChartInfo, Data, UserInfo  # noqa: E402


def _charts(count: int = 20) -> list[ChartInfo]:
    return [
        ChartInfo(
            achievements=100.0,
            fc="",
            fs="",
            level="14",
            levelIndex=3,
            level_label="Master",
            title=f"Song {index}",
            type="DX",
            ds=14.0,
            dxScore=0,
            rating=300,
            rate="sss",
            song_id=index + 1,
        )
        for index in range(count)
    ]


def main() -> None:
    charts = _charts()
    user = UserInfo(
        additional_rating=0,
        nickname="owner",
        rating=15000,
        username="owner",
        charts=Data(sd=charts, dx=[]),
    )
    # 内鬼来源：另一位群友，曲目 song_id 与 owner 不重合
    alien_charts = [
        ChartInfo(
            achievements=99.5,
            fc="",
            fs="",
            level="14",
            levelIndex=3,
            level_label="Master",
            title=f"Alien {index}",
            type="DX",
            ds=13.8,
            dxScore=0,
            rating=280,
            rate="ssp",
            song_id=1000 + index,
        )
        for index in range(20)
    ]
    alien = UserInfo(
        additional_rating=0,
        nickname="alien",
        rating=14200,
        username="alien",
        charts=Data(sd=alien_charts, dx=[]),
    )

    assert [RATING_DIFFICULTIES[i].display_count for i in range(1, 6)] == [20, 16, 12, 8, 8]
    assert RATING_DIFFICULTIES[5].hide_cover is True
    assert RATING_DIFFICULTIES[4].hide_cover is False
    random.seed(7)
    cards, answer = build_impostor_cards(user, alien)
    assert len(cards) == 5
    assert 1 <= answer <= 5
    # 内鬼那张的 song_id 来自 alien_charts；其余来自 user.charts
    alien_song_ids = {c.song_id for c in alien_charts}
    user_song_ids = {c.song_id for c in charts}
    assert cards[answer - 1].song_id in alien_song_ids
    for idx, card in enumerate(cards):
        if idx + 1 == answer:
            continue
        assert card.song_id in user_song_ids

    rating = GuessRatingManager()
    rating.groups = {}
    rating.locked = set()
    rating.start(
        1,
        target_uid=100,
        target_name="owner",
        target_rating=15000,
        difficulty=4,
        display_count=8,
        total_chart_count=len(charts),
        duration=60,
        selected_charts=charts[:8],
        b50_sd=charts,
        b50_dx=[],
    )
    # 题主也能作答获得回应（结算再排除）
    assert rating.submit(1, "owner", "owner", 100, 15000).startswith("✅")
    rating.submit(1, "a", "A", 101, 14990)
    rating.submit(1, "b", "B", 102, 14980)
    rating.submit(1, "c", "C", 103, 14970)
    rating_settlement = rating.settle(1)
    assert rating_settlement is not None
    assert [(r.score, r.break_points) for r in rating_settlement.rewards] == [
        (24, 5), (8, 2), (6, 1),
    ]

    impostor = GuessImpostorManager()
    impostor.groups = {}
    impostor.locked = set()
    impostor.start(
        2,
        target_uid=200,
        target_name="owner",
        alien_uid=300,
        alien_name="alien",
        answer=answer,
        charts=cards,
    )
    # 题主 / 内鬼 submit 有回应但结算时被过滤
    assert impostor.submit(2, "owner", "owner", 200, answer).startswith("✅")
    assert impostor.submit(2, "alien", "alien", 300, answer).startswith("✅")
    impostor.submit(2, "a", "A", 201, answer)
    impostor.submit(2, "b", "B", 202, 1 if answer != 1 else 2)
    impostor.submit(2, "c", "C", 203, answer)
    impostor_settlement = impostor.settle(2)
    assert impostor_settlement is not None
    assert [
        (r.name, r.score, r.break_points) for r in impostor_settlement.rewards
    ] == [("A", 10, 2), ("C", 6, 1)]
    assert impostor_settlement.wrong_names == ["B"]

    modes = GuessScoreManager.GUESS_MODES
    assert modes[-1] == GuessScoreManager.MODE_IMPOSTOR
    stats_image = draw_personal_guess_stats({
        "uid": "1",
        "name": "render-test",
        "total_score": 70,
        "total_rank": 1,
        "period_snapshot": {
            key: (10, 1) for key in ("daily", "weekly", "monthly", "season")
        },
        "modes": {
            mode: {"count": 1, "points": 10, "last_at": "07-28 09:30"}
            for mode in modes
        },
        "daily_series": {
            "labels": ["07-27", "07-28"],
            **{mode: [0, 10] for mode in modes},
        },
        "recent": [
            {"mode": mode, "points": 10, "at": "07-28 09:30"}
            for mode in modes
        ],
        "radar": {
            "labels": [GuessScoreManager.MODE_LABELS[mode] for mode in modes],
            "modes": list(modes),
            "points": [10] * len(modes),
            "counts": [1] * len(modes),
        },
        "note": "render test",
    })
    assert stats_image.width == 1080 and stats_image.height > 1600
    print("guess rating/impostor: OK")


if __name__ == "__main__":
    main()
