"""Regression checks for AWMC NET. fallback, trend, and one-time notice."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import nonebot

nonebot.init(maimaidxpath="/tmp/maimaidx-awmcnet-test")

from nonebot_plugin_maimaidx.libraries import maimaidx_awmcnet_sync as awmcnet
from nonebot_plugin_maimaidx.libraries import maimaidx_datasource as datasource
from nonebot_plugin_maimaidx.libraries.maimaidx_account_db import AccountDatabase


RECORD = {
    "song_id": 1,
    "title": "Test",
    "type": "SD",
    "level_index": 3,
    "level_label": "Master",
    "level": "13",
    "ds": 13.0,
    "achievements": 100.0,
    "dxScore": 2000,
    "ra": 280,
    "rate": "sss",
    "fc": "fc",
    "fs": "fs",
}
PLAYER = {
    "qq": 12345,
    "nickname": "Tester",
    "username": "bot_12345",
    "rating": 280,
    "records": [RECORD],
}


async def test_datasource() -> None:
    original_fetch = awmcnet.fetch_awmcnet_player
    original_refresh = datasource._refresh_awmcnet_from_upstreams
    try:
        awmcnet.fetch_awmcnet_player = AsyncMock(return_value=PLAYER)
        datasource._refresh_awmcnet_from_upstreams = AsyncMock(
            side_effect=AssertionError("normal AWMC NET query touched an upstream")
        )
        user, records = await datasource._get_awmcnet_records(12345)
        assert user.nickname == "Tester" and len(records) == 1
        assert datasource._refresh_awmcnet_from_upstreams.await_count == 0

        awmcnet.fetch_awmcnet_player = AsyncMock(side_effect=[None, PLAYER])
        datasource._refresh_awmcnet_from_upstreams = AsyncMock(
            return_value=(user, records)
        )
        _, migrated = await datasource._get_awmcnet_records(12345)
        assert len(migrated) == 1
        assert datasource._refresh_awmcnet_from_upstreams.await_count == 1
    finally:
        awmcnet.fetch_awmcnet_player = original_fetch
        datasource._refresh_awmcnet_from_upstreams = original_refresh


def test_first_notice() -> None:
    with TemporaryDirectory() as directory:
        db = AccountDatabase(Path(directory) / "account.db")
        assert db.mark_awmcnet_notified_once("12345") is True
        assert db.mark_awmcnet_notified_once("12345") is False


def test_trend_text() -> None:
    text = awmcnet.format_awmcnet_trend({
        "nickname": "Milk",
        "days": 30,
        "points": [
            {
                "date": "2026-07-31",
                "rating": 14480,
                "delta": 0,
                "old_rating": 10120,
                "new_rating": 4360,
                "record_count": 505,
            },
            {
                "date": "2026-08-01",
                "rating": 14509,
                "delta": 29,
                "old_rating": 10140,
                "new_rating": 4369,
                "record_count": 510,
            },
        ],
        "summary": {
            "start_rating": 14480,
            "end_rating": 14509,
            "rating_delta": 29,
            "days_with_data": 2,
        },
    })
    assert "14509" in text and "+29" in text and "B35 / B15" in text


async def test_empty_sync_is_not_reported_as_success() -> None:
    original_connection = awmcnet._connection
    try:
        awmcnet._connection = lambda: ("https://net.wmc.pub", "test", 8.0)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "status": "ok", "imported": 0, "updated": 0,
            "skipped": 1, "stored_records": 0,
            "errors": ["找不到歌曲"],
        }
        client = AsyncMock()
        client.post.return_value = response
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        with patch.object(awmcnet.httpx, 'AsyncClient', return_value=context):
            result = await awmcnet._post_sync({"qq": 12345, "records": [RECORD]})
        assert result is None
    finally:
        awmcnet._connection = original_connection


asyncio.run(test_datasource())
asyncio.run(test_empty_sync_is_not_reported_as_success())
test_first_notice()
test_trend_text()
print("AWMC NET integration tests: ok")
