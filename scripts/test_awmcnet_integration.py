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


def test_sync_payload_dedupes_duplicate_charts_and_strips_token() -> None:
    original_token = getattr(awmcnet.maiconfig, "awmcnet_bot_token", None)
    try:
        awmcnet.maiconfig.awmcnet_bot_token = "  shared-token\n"
        assert awmcnet._connection()[1] == "shared-token"
        weaker = {**RECORD, "achievements": 99.5, "dxScore": 1000}
        stronger = {**RECORD, "achievements": 100.0, "dxScore": 2200}
        payload = awmcnet._dedupe_record_payloads([weaker, stronger])
        assert len(payload) == 1
        assert payload[0]["achievements"] == 100.0
        assert payload[0]["dxScore"] == 2200
    finally:
        awmcnet.maiconfig.awmcnet_bot_token = original_token


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


async def test_force_refresh_keeps_newer_awmcnet_snapshot() -> None:
    """A stale upstream must not overwrite a score uploaded moments ago."""
    fresh_record = {**RECORD, "achievements": 100.0, "dxScore": 2500}
    stale_record = {**RECORD, "achievements": 99.0, "dxScore": 1000}
    fresh_player = {**PLAYER, "records": [fresh_record]}
    stale_player = {**PLAYER, "records": [stale_record]}
    original_fetch = awmcnet.fetch_awmcnet_player
    original_refresh = datasource._refresh_awmcnet_from_upstreams
    original_upstream_fetch = datasource.get_user_records
    original_sync = awmcnet.sync_awmcnet
    try:
        awmcnet.fetch_awmcnet_player = AsyncMock(
            side_effect=[fresh_player, stale_player]
        )
        stale_user, stale_records = datasource._records_to_userinfo(
            stale_player, datasource._awmcnet_records(stale_player)
        ), datasource._awmcnet_records(stale_player)
        datasource.get_user_records = AsyncMock(
            return_value=(stale_user, stale_records)
        )
        awmcnet.sync_awmcnet = AsyncMock(return_value={"stored_records": 1})

        _userinfo, records = await datasource._get_awmcnet_records(
            12345, force_refresh=True
        )
        assert records
        assert max(float(row.achievements) for row in records) == 100.0
        assert awmcnet.sync_awmcnet.await_count == 1
    finally:
        awmcnet.fetch_awmcnet_player = original_fetch
        datasource._refresh_awmcnet_from_upstreams = original_refresh
        datasource.get_user_records = original_upstream_fetch
        awmcnet.sync_awmcnet = original_sync


asyncio.run(test_datasource())
asyncio.run(test_empty_sync_is_not_reported_as_success())
asyncio.run(test_force_refresh_keeps_newer_awmcnet_snapshot())
test_first_notice()
test_trend_text()
test_sync_payload_dedupes_duplicate_charts_and_strips_token()
print("AWMC NET integration tests: ok")
