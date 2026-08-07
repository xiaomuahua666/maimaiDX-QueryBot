#!/usr/bin/env python3
"""Regression checks for the Feishu operations bot."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("feishu_ops_bot", ROOT / "scripts" / "feishu_ops_bot.py")
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

assert MODULE.parse_id_set("a,b c;a") == frozenset({"a", "b", "c"})
assert MODULE.extract_text('{"text":"@_user_1  日志 50"}') == "日志 50"
assert MODULE.parse_command("状态") == ("status", [])
assert MODULE.parse_command("发放BREAK 12345 100") == (
    "break_add",
    ["12345", "100"],
)
assert MODULE.parse_command("查询REF REF-ABCDEF1234567890") == (
    "ref_query",
    ["REF-ABCDEF1234567890"],
)

raw = (
    "2026 INFO token=abc ROBOT1.0_secret QQ 123456 "
    "ou_abcdef [Group:ABCDEF] Authorization: Bearer xyz "
    "https://example.test/callback?code=oauth-code"
)
redacted = MODULE.redact_log_line(raw)
for secret in (
    "abc",
    "ROBOT1.0_secret",
    "123456",
    "ou_abcdef",
    "ABCDEF",
    "xyz",
    "oauth-code",
):
    assert secret not in redacted, secret
assert redacted.count("[REDACTED]") >= 5

message_line = "2026 [SUCCESS] nonebot | QQ 123 | [EventType.GROUP_MESSAGE]: Message x"
assert MODULE.sanitize_logs([message_line, "2026 INFO startup complete"]) == [
    "2026 INFO startup complete"
]
assert MODULE.sanitize_logs(
    ["2026 INFO ok", "2026 ERROR failed"], errors_only=True
) == ["2026 ERROR failed"]
assert MODULE.sanitize_logs(
    [
        "2026 WARNING nonebot_plugin_maimaidx | [wmc] API 非 200 path=/charts/x status=404",
        "2026 INFO Event will be handled by Matcher(type='message')",
        "2026 INFO useful processing log",
    ]
) == ["2026 INFO useful processing log"]

status = {
    "state": "running",
    "active_state": "active",
    "sub_state": "running",
    "result": "success",
    "supervisor_pid": 10,
    "bot_pid": 20,
    "uptime_seconds": 3661,
    "cpu_percent": 1.2,
    "rss_kib": 102400,
    "disk_percent": 42.5,
    "load_1": 0.1,
    "load_5": 0.2,
    "load_15": 0.3,
    "deployed_commit": "0123456789abcdef",
    "n_restarts": 0,
    "qq_connected": True,
}
member_card = MODULE.status_card(status, is_admin=False)
admin_card = MODULE.status_card(status, is_admin=True)
assert member_card["header"]["template"] == "green"
assert "1小时 1分钟" in member_card["elements"][0]["text"]["content"]
assert "`" not in member_card["elements"][0]["text"]["content"]
waiting_card = MODULE.status_card({**status, "qq_connected": False}, is_admin=False)
assert waiting_card["header"]["template"] == "orange"
assert "等待 QQ 连接" in waiting_card["elements"][0]["text"]["content"]
member_buttons = member_card["elements"][2]["actions"]
admin_buttons = admin_card["elements"][2]["actions"]
assert len(member_buttons) == 4
assert len(admin_buttons) == 5
assert member_buttons[-1]["value"]["action"] == "menu"

message_card = MODULE.message_stats_card(
    [
        {"group_id": "100", "user_id": "200", "messages": 3},
        {"group_id": "100", "user_id": "201", "messages": 2},
        {"group_id": "encrypted", "user_id": "ABCDEF", "messages": 999},
    ],
    days=7,
)
message_text = message_card["elements"][0]["text"]["content"]
assert "统计窗口：最近 7 天" in message_text
assert "QQ 200：3 条" in message_text
assert "ABCDEF" not in message_text
assert "100：5 条" in message_text
assert message_card["elements"][-1]["actions"][-1]["value"]["action"] == "menu"

quota_card = MODULE.api_report_card(
    {
        "days": 1,
        "calls": 10,
        "success": 9,
        "errors": 1,
        "not_found_404": 0,
        "quota": {
            "exceeded": 1,
            "observed": 1,
            "latest": {"scope": "personal", "category": "read", "window": "1h", "used": 10, "limit": 10},
        },
        "paths": [],
    },
    {"awmc_api_mode": "public", "awmc_api_token_configured": True},
)
quota_text = quota_card["elements"][0]["text"]["content"]
assert "限额触发：1 次" in quota_text
assert "使用 10/10" in quota_text

trace_card = MODULE.ref_card(
    {
        "ref_id": "REF-ABCDEF1234567890",
        "command": "b50",
        "user_id": "123456789",
        "group_id": "987654321",
        "status": "success",
        "duration_ms": 1250,
        "input_summary": '{"request":"b50 12","message_length":5}',
        "steps": [
            {
                "step_name": "http.awmc",
                "status": "success",
                "duration_ms": 500,
                "detail": '{"path":"/v1/user/data","status_code":200}',
            }
        ],
    },
    "REF-ABCDEF1234567890",
)
trace_text = trace_card["elements"][0]["text"]["content"]
assert "触发人：123456789" in trace_text
assert "请求摘要" in trace_text
assert "返回摘要" in trace_text

confirm = MODULE.confirmation_card(
    "确认", "发放 100 BREAK", "break_confirm", user_id="123", amount=100
)
value = confirm["elements"][2]["actions"][0]["value"]
assert value["action"] == "break_confirm"
assert len(value["request_id"]) == 32

with tempfile.TemporaryDirectory() as directory:
    store = MODULE.ActionStore(Path(directory) / "actions.sqlite3")
    assert store.claim("request-1", "break.update", "ou_admin") is True
    assert store.claim("request-1", "break.update", "ou_admin") is False
    assert store.is_admin("ou_admin") is False
    store.grant_admin("ou_admin", "ou_super")
    assert store.is_admin("ou_admin") is True
    assert store.revoke_admin("ou_admin") is True
    assert store.is_admin("ou_admin") is False

admin_source = (ROOT / "libraries" / "maimaidx_admin_web.py").read_text(
    encoding="utf-8"
)
for required in (
    '"feishu_ops"',
    'body.get("actor")',
    'body.get("source")',
    'globals()["Request"] = Request',
):
    assert required in admin_source, required

bot_source = (ROOT / "scripts" / "feishu_ops_bot.py").read_text(encoding="utf-8")
assert '"im.message.reaction.created_v1"' in bot_source
assert "register_p2_customized_event" in bot_source

print("Feishu operations bot checks: OK")
