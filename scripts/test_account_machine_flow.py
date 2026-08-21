"""账号状态兼容解析与落雪 OAuth 刷新回归测试（无需启动 NoneBot）。"""

import ast
import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent.parent


def load_functions(path: Path, names: set[str], namespace: dict) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert {node.name for node in selected} == names
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


test_config = SimpleNamespace(awmc_ticket_allowed_multipliers="2,3,5")
account = load_functions(
    ROOT / "command" / "mai_account.py",
    {
        "_nested_preview",
        "_merged_preview",
        "_pick",
        "_normalize_preview",
        "_normalize_charge_payload",
        "_ticket_stock",
        "_unused_ticket_stocks",
        "_ticket_valid_timestamp",
        "_format_ticket_status",
        "_allowed_ticket_multipliers",
        "auto_upload_channels",
        "_exception_detail",
        "_upload_failure_message",
        "_result_text",
    },
    {
        "Any": Any,
        "Optional": Optional,
        "maiconfig": test_config,
        "asyncio": asyncio,
        "time": time,
        "httpx": __import__("httpx"),
        "re": re,
        "json": json,
        "redact": lambda value: value,
        "find_sw_api_error": lambda _exc: None,
        "format_sw_api_quota_error": lambda error: str(error),
        "SwApiError": type("SwApiError", (Exception,), {}),
    },
)

assert "请求超时" in account["_upload_failure_message"](TimeoutError())
assert account["_result_text"](
    {"count": 28, "skipped": [{"musicId": 181, "reason": "missing"}]}
) == "已处理 28 条成绩"
assert "RuntimeError（上游服务未返回错误详情）" in account[
    "_upload_failure_message"
](RuntimeError())

assert account["_allowed_ticket_multipliers"]() == (2, 3, 5)
assert account["auto_upload_channels"]() == (False, False)
assert account["auto_upload_channels"](fish_token="fish") == (True, False)
assert account["auto_upload_channels"](lxns_token="lx") == (False, True)
assert account["auto_upload_channels"](has_lxns_oauth=True) == (False, True)
assert account["auto_upload_channels"](has_fish_oauth=True) == (False, False)
assert account["auto_upload_channels"](
    fish_token="old", divingfish_oauth_mode=True
) == (True, False)
assert account["auto_upload_channels"](
    has_fish_oauth=True, divingfish_oauth_mode=True
) == (True, False)
assert account["auto_upload_channels"](
    fish_token="fish", has_lxns_oauth=True
) == (True, True)

# OAuth is required for reads, but a legacy waterfish Import-Token remains a
# valid upload credential when no grant is available.
channel_tree = ast.parse(
    (ROOT / "command" / "mai_account.py").read_text(encoding="utf-8")
)
channel_nodes = [
    node for node in channel_tree.body
    if (
        isinstance(node, ast.ClassDef) and node.name == "_UploadChannels"
    ) or (
        isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_resolve_upload_channels"
    )
]
assert len(channel_nodes) == 2


class FakeFishNotAuthorized(Exception):
    pass


class FakeFishOAuthError(Exception):
    pass


class FakeLog:
    def warning(self, _message):
        pass


async def denied_fish_token(_qqid):
    raise FakeFishNotAuthorized


channel_namespace = {
    "AccountBinding": Any,
    "MessageEvent": Any,
    "Optional": Optional,
    "dataclass": dataclass,
    "divingfish_oauth_enabled": lambda: True,
    "get_divingfish_access_token": denied_fish_token,
    "DivingFishNotAuthorizedError": FakeFishNotAuthorized,
    "DivingFishOAuthError": FakeFishOAuthError,
    "_oauth_qqid": lambda _event: 12345,
    "_user_key": lambda _event: "12345",
    "_lxns_oauth_access_token": lambda _event: None,
    "_has_lxns_oauth": lambda _event: False,
    "_lxns_oauth_missing_write_scope": lambda _event: False,
    "log": FakeLog(),
}
exec(
    compile(ast.Module(body=channel_nodes, type_ignores=[]), "mai_account.py", "exec"),
    channel_namespace,
)
binding = SimpleNamespace(fish_token="legacy-token", lxns_token="")
channels = asyncio.run(
    channel_namespace["_resolve_upload_channels"](
        object(), binding, fish=True, lxns=False
    )
)
assert channels.fish
assert not channels.lxns
assert not channels.fish_oauth
assert "Import-Token 上传" in channels.warnings[0]
assert "读取成绩仍需" in channels.warnings[0]


async def valid_fish_token(_qqid):
    return "access-token"


channel_namespace["get_divingfish_access_token"] = valid_fish_token
channels = asyncio.run(
    channel_namespace["_resolve_upload_channels"](
        object(), binding, fish=True, lxns=False
    )
)
assert channels.fish
assert channels.fish_oauth
assert channels.warnings == ()

test_config.awmc_ticket_allowed_multipliers = "3，5, 7,invalid"
assert account["_allowed_ticket_multipliers"]() == (3, 5)
test_config.awmc_ticket_allowed_multipliers = ""
assert account["_allowed_ticket_multipliers"]() == (2, 3, 5)

uid, name, rating, preview = account["_normalize_preview"](
    {
        "userId": 123456,
        "banState": 2,
        "returnCode": 1,
        "userData": {
            "userName": "TEST",
            "playerRating": 15000,
            "playCount": 123,
        },
    }
)
assert (uid, name, rating) == ("123456", "TEST", 15000)
assert preview["banState"] == 2
assert preview["playCount"] == 123

ok, tickets, free_tickets = account["_normalize_charge_payload"](
    {
        "returnCode": 1,
        "userCharge": {
            "userChargeList": [{"chargeId": 2, "stock": 1}],
            "userFreeChargeList": [{"chargeId": 1, "stock": 2}],
        },
    }
)
assert ok
assert tickets[0]["chargeId"] == 2
assert free_tickets[0]["stock"] == 2
# 新接口无票时列表可能为 null，但 userId + list 字段仍代表有效响应。
ok, tickets, free_tickets = account["_normalize_charge_payload"](
    {"userId": 123456, "length": 0, "userChargeList": None}
)
assert ok and tickets == [] and free_tickets == []
assert account["_ticket_stock"](
    [{"chargeId": 2, "stock": 1}, {"ChargeId": "2", "Stock": "2"}], 2
) == 3
assert account["_unused_ticket_stocks"](
    [
        {"chargeId": 2, "stock": 1},
        {"chargeId": 3, "stock": 8, "validDate": "2000-01-01 00:00:00"},
        {"chargeId": 5, "stock": 2},
        {"chargeId": 7, "stock": 99},
    ],
    now=time.mktime(time.strptime("2026-01-01", "%Y-%m-%d")),
) == {2: 1, 5: 2}

ticket_text = account["_format_ticket_status"](
    {
        "userId": 987654321,
        "qrcode": "SGWCMAID-SECRET",
        "returnCode": 1,
        "userChargeList": [
            {"chargeId": 2, "stock": 1, "validDate": "2099-01-02 03:04:05"},
            {"chargeId": 3, "stock": 0},
        ],
        "userFreeChargeList": [{"chargeId": 1, "stock": 2}],
    },
    now=time.mktime(time.strptime("2099-01-01", "%Y-%m-%d")),
)
assert "有效票券共 3 张" in ticket_text
assert "2 倍票 × 1" in ticket_text and "免费票券 × 2" in ticket_text
assert "987654321" not in ticket_text and "userId" not in ticket_text
assert "SGWCMAID" not in ticket_text and "qrcode" not in ticket_text
assert account["_format_ticket_status"](
    {"userId": 123456, "length": 0, "userChargeList": None}
) == "🎫 舞萌票券状态\n当前没有有效票券。"


class FakeLxnsDb:
    def __init__(self):
        self.saved = None

    def update_tokens(self, qqid: int, **values):
        self.saved = (qqid, values)


async def fake_refresh_token(_token: str) -> dict:
    # 模拟服务端只返回新 access_token，不轮换 refresh_token/scope。
    return {"access_token": "new-access", "expires_in": 900}


fake_db = FakeLxnsDb()
lxns = load_functions(
    ROOT / "command" / "mai_lxns.py",
    {"_do_token_refresh"},
    {
        "Optional": Optional,
        "refresh_token": fake_refresh_token,
        "lxns_db": fake_db,
        "log": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    },
)
token = asyncio.run(
    lxns["_do_token_refresh"](
        10001,
        {
            "refresh_token": "old-refresh",
            "scope": "read_player write_player",
            "token_type": "Bearer",
        },
    )
)
assert token == "new-access"
assert fake_db.saved is not None
assert fake_db.saved[1]["refresh_token"] == "old-refresh"
assert fake_db.saved[1]["scope"] == "read_player write_player"

# OAuth 上传先解析真实渠道，并复用写入 AWMCNET 的同一份全量成绩。
upload_src = (ROOT / "command" / "mai_account.py").read_text(encoding="utf-8")
assert 'lines.extend(["", _format_ticket_status(charge)])' in upload_src
assert 'if isinstance(matcher, upload_fish):' in upload_src
assert 'if isinstance(matcher, upload_lx):' in upload_src
assert 'if isinstance(matcher, upload_all):' in upload_src
assert 'stored = matcher.state.get(_UPLOAD_MODE_STATE_KEY)' in upload_src
assert "async def _resolve_upload_channels(" in upload_src
assert "本次使用已绑定的 Import-Token 上传" in upload_src
assert "OAuth Token 已失效且自动刷新失败" in upload_src
assert "A broken OAuth grant must never silently fall back" in upload_src
assert "lxns_scores = convert_sega_music_scores(raw_scores)" in upload_src
assert "fish_records = convert_sega_music_scores_to_divingfish(raw_scores)" in upload_src
assert "results.extend(external_warnings)" in upload_src
assert "filter_anomalous_scores" not in upload_src
fallback_block = (
    "if not binding.lxns_token:\n"
    "                        raise RuntimeError(\n"
    "                            _lxns_upload_failure_text(exc, stage=lxns_stage)\n"
    "                            + \"。请修正后重新发送 lxbind 授权并重试\"\n"
    "                        ) from exc\n"
    "                    await wait_between_machine_steps()\n"
    "                    result = await sw_api.update_lx(qrcode, binding.lxns_token)"
)
assert fallback_block not in upload_src

sw_api_src = (ROOT / "libraries" / "maimaidx_sw_api.py").read_text(encoding="utf-8")
assert "awmc_user_music_timeout_seconds" in sw_api_src
assert "awmc_user_items_timeout_seconds" in sw_api_src
assert "awmc_b50_upload_timeout_seconds" in sw_api_src
assert "_b50_upload_timeout" in sw_api_src
# B50 上传（水鱼 + 落雪）统一 120s 硬超时，零重试。
assert "upload_timeout = self._b50_upload_timeout()" in sw_api_src
assert "retry_count=0" in sw_api_src

config_src = (ROOT / "config.py").read_text(encoding="utf-8")
assert "awmc_b50_upload_timeout_seconds: float = 120.0" in config_src
assert "awmc_upload_poll_timeout_seconds: float = 120.0" in config_src
assert "awmc_user_music_timeout_seconds: float = 15.0" in config_src
assert "awmc_user_items_timeout_seconds: float = 120.0" in config_src
assert "awmc_lxns_pc_cache_seconds" in config_src
assert "_lxns_scores_from_pc_cache" in upload_src
assert "convert_pc_records_to_lxns_scores" in upload_src
assert "PC缓存" in upload_src
assert "binding, _ = await _read_verified_preview(" in upload_src
assert "before_charge = await sw_api.get_user_charge(binding.qrcode)" in upload_src
assert "return await _confirm_ticket_delivery(" in upload_src
assert "verified_stock, attempts = await _run_ticket_with_retries(" in upload_src
assert "raise UnusedTicketPenaltyError(unused_stocks)" in upload_src
assert '"ticket_unused_penalty"' in upload_src
assert "你智商可好？发一堆票和意为？已吃掉" in upload_src

break_src = (ROOT / "command" / "mai_break.py").read_text(encoding="utf-8")
assert "'我的awmc'" in break_src
assert "'awmc状态'" in break_src
assert "def _upload_preflight_error(" in upload_src
preflight_pos = upload_src.index("preflight_error = _upload_preflight_error(")
request_pos = upload_src.index("result = await _upload(", preflight_pos)
assert preflight_pos < request_pos
assert "await matcher.finish(preflight_error, reply_message=False)" in upload_src
assert "async def _notify_upload_accepted(" in upload_src
assert "📤 已受理，正在上传到" in upload_src
assert "def _upload_can_start_now(" in upload_src
ready_pos = upload_src.index("if _upload_can_start_now(")
notify_pos = upload_src.index("await _notify_upload_accepted(", ready_pos)
request_pos = upload_src.index("result = await _upload(", notify_pos)
assert ready_pos < notify_pos < request_pos
assert "fish=channels.fish, lxns=channels.lxns" in upload_src[ready_pos:request_pos]
assert "_channels=channels" in upload_src[request_pos:]
assert "format_processing_estimate(seconds, samples)" in upload_src

lxns_client_src = (ROOT / "libraries" / "maimaidx_lxns_client.py").read_text(
    encoding="utf-8"
)
assert "read=15.0" in lxns_client_src
assert "overall_deadline" in lxns_client_src
assert "convert_pc_records_to_lxns_scores" in lxns_client_src

print("account machine flow tests: ok")
