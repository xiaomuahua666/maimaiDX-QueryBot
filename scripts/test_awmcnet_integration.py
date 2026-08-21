"""Regression checks for AWMC NET. fallback, trend, and one-time notice."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import nonebot

nonebot.init(maimaidxpath="/tmp/maimaidx-awmcnet-test")

from nonebot_plugin_maimaidx.libraries import maimaidx_awmcnet_sync as awmcnet
from nonebot_plugin_maimaidx.libraries import maimaidx_datasource as datasource
from nonebot_plugin_maimaidx.libraries import maimaidx_score_filter as score_filter
from nonebot_plugin_maimaidx.libraries.maimaidx_model import ChartInfo, Data, UserInfo
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
        assert result.status is awmcnet.AwmcnetSyncStatus.REJECTED
        assert not result.ok
    finally:
        awmcnet._connection = original_connection


async def test_sync_retries_uploading_429() -> None:
    """A transient 'upload in progress' response must not fail the upload."""
    original_connection = awmcnet._connection
    try:
        awmcnet._connection = lambda: ("https://net.wmc.pub", "test", 8.0)
        busy = MagicMock()
        busy.status_code = 429
        busy.text = '{"detail":"成绩正在上传中"}'
        busy.headers = {"Retry-After": "0"}
        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"status": "ok", "stored_records": 1}
        client = AsyncMock()
        client.post.side_effect = [busy, success]
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        with (
            patch.object(awmcnet.httpx, "AsyncClient", return_value=context),
            patch.object(awmcnet.asyncio, "sleep", new=AsyncMock()),
        ):
            result = await awmcnet._post_sync({"qq": 12345, "records": [RECORD]})
        assert result.status is awmcnet.AwmcnetSyncStatus.SUCCESS
        assert result.payload == {"status": "ok", "stored_records": 1}
        assert client.post.await_count == 2
    finally:
        awmcnet._connection = original_connection


async def test_sync_read_timeout_is_ambiguous_not_auth_failure() -> None:
    """A timeout after sending the request must not claim the Bot-Token is wrong."""
    original_connection = awmcnet._connection
    try:
        awmcnet._connection = lambda: ("https://net.wmc.pub", "test", 8.0)
        client = AsyncMock()
        client.post.side_effect = httpx.ReadTimeout("timed out waiting for response")
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        with patch.object(awmcnet.httpx, "AsyncClient", return_value=context):
            result = await awmcnet._post_sync({"qq": 12345, "records": [RECORD]})
        assert result.status is awmcnet.AwmcnetSyncStatus.AMBIGUOUS
        assert result.ambiguous
        assert result.payload is None
        assert client.post.await_count == 1
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


async def test_anomalous_scores_are_filtered_at_datasource_boundaries() -> None:
    bad_raw = {
        **RECORD,
        "achievements": 101.0,
        "fc": "app",
        "dxScore": 3000,
        "dxScoreMax": 3000,
    }
    converted = datasource._awmcnet_records({"records": [RECORD, bad_raw]})
    assert len(converted) == 1

    good = SimpleNamespace(
        song_id=1, level_index=3, achievements=100.0, dxScore=2000,
        fc="fc", ra=280,
    )
    bad = SimpleNamespace(
        song_id=2, level_index=3, achievements=101.0, dxScore=3000,
        dxScoreMax=3000, fc="app", ra=999,
    )
    merged_user, merged_records = datasource._merge_upstream_records([
        (UserInfo(
            nickname="Tester", rating=100, additional_rating=0,
            username="tester", charts=None,
        ), [good, bad]),
    ])
    assert merged_user is not None and [row.song_id for row in merged_records] == [1]

    original_get_source = datasource.get_user_source
    original_fetch = datasource._get_user_records_from_source
    original_b50_fetch = datasource._get_user_b50_from_source
    original_predicate = score_filter.is_anomalous_perfect_score
    calls: list[str] = []

    async def fake_records(*args, source, **kwargs):
        calls.append(source)
        if source == "lxns":
            raise datasource.LxnsDataError("upstream unavailable")
        return UserInfo(
            nickname="Tester", rating=100, additional_rating=0,
            username="tester", charts=None,
        ), [good, bad]

    def fake_anomaly(record, **kwargs):
        return getattr(record, "song_id", None) == 2

    chart_good = ChartInfo(
        song_id=1, title="Good", level_label="Master", level_index=3,
        achievements=100.0, dxScore=2000, fc="fc", ra=280,
    )
    chart_bad = ChartInfo(
        song_id=2, title="Bad", level_label="Master", level_index=3,
        achievements=101.0, dxScore=3000, fc="app", ra=999,
    )
    b50_user = UserInfo(
        nickname="Tester", rating=999, additional_rating=0,
        username="tester",
        charts=Data(sd=[chart_good, chart_bad], dx=[]),
    )

    async def fake_b50(*args, **kwargs):
        return b50_user

    try:
        datasource.get_user_source = lambda _qqid: "lxns"
        datasource._get_user_records_from_source = fake_records
        user, records = await datasource.get_user_records(qqid=12345)
        assert user.nickname == "Tester" and [row.song_id for row in records] == [1]
        assert calls == ["lxns", "awmcnet"]

        score_filter.is_anomalous_perfect_score = fake_anomaly
        datasource._get_user_b50_from_source = fake_b50
        b50 = await datasource.get_user_b50(qqid=12345, force_source="awmcnet")
        chart_ids = [row.song_id for row in (b50.charts.sd or []) + (b50.charts.dx or [])]
        assert chart_ids == [1]
    finally:
        datasource.get_user_source = original_get_source
        datasource._get_user_records_from_source = original_fetch
        datasource._get_user_b50_from_source = original_b50_fetch
        score_filter.is_anomalous_perfect_score = original_predicate


asyncio.run(test_datasource())
asyncio.run(test_empty_sync_is_not_reported_as_success())
asyncio.run(test_sync_retries_uploading_429())
asyncio.run(test_force_refresh_keeps_newer_awmcnet_snapshot())
asyncio.run(test_anomalous_scores_are_filtered_at_datasource_boundaries())
test_first_notice()
test_trend_text()
test_sync_payload_dedupes_duplicate_charts_and_strips_token()
print("AWMC NET integration tests: ok")
