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
assert "平均每秒消息：**0.000008** 条" in message_text
assert "平均每分钟消息：**0.000496** 条" in message_text
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

# --- ban_list command ----------------------------------------------------
assert MODULE.parse_command("封禁列表") == ("ban_list", [])
assert MODULE.parse_command("封禁列表 全部") == ("ban_list", ["全部"])

empty_bans = MODULE.bans_card([], all_bans=False)
assert empty_bans["header"]["title"]["content"] == "封禁列表（生效中）"
assert "暂无封禁记录" in empty_bans["elements"][0]["text"]["content"]
all_bans_card = MODULE.bans_card([], all_bans=True)
assert all_bans_card["header"]["title"]["content"] == "封禁列表（全部）"

ban_rows = [
    {
        "user_id": "123",
        "reason": "滥用接口",
        "actor": "ou_super",
        "created_at": 1700000000.0,
        "expires_at": None,
        "active": 1,
    },
    {
        "user_id": "456",
        "reason": "测试",
        "actor": "ou_admin",
        "created_at": 1700000000.0,
        "expires_at": 1700003600.0,
        "active": 1,
    },
]
populated = MODULE.bans_card(ban_rows, all_bans=False)
content = populated["elements"][0]["text"]["content"]
assert "123" in content
assert "456" in content
assert "滥用接口" in content
assert "永久" in content
assert "共 **2** 条" in content

inactive_rows = [
    {
        "user_id": "789",
        "reason": "已解封",
        "actor": "ou_admin",
        "created_at": 1700000000.0,
        "expires_at": None,
        "active": 0,
    }
]
inactive_card = MODULE.bans_card(inactive_rows, all_bans=True)
assert "已解封/过期" in inactive_card["elements"][0]["text"]["content"]

admin_api_source = (ROOT / "libraries" / "maimaidx_admin_web.py").read_text(
    encoding="utf-8"
)
assert "async def bans(" in admin_api_source
assert "/bans" in admin_api_source

# ── 日志翻页快照逻辑 ──
LogSnapshot = MODULE.LogSnapshot
import time as _time  # noqa: E402

# 250 行 → 3 页（100+100+50），page 0 = 最新一页 = 末尾 100 行
snap = LogSnapshot(
    lines=[f"line{i}" for i in range(250)],
    errors_only=False,
    since_today_6=True,
    fetched_at=_time.time(),
)
assert snap.total_pages() == 3, f"250行应3页: {snap.total_pages()}"
assert snap.current_lines()[0] == "line150", f"page0应从line150开始: {snap.current_lines()[0]}"
assert snap.current_lines()[-1] == "line249"
# 上一页（更早）→ page1 = line50..149
assert snap.go_prev() is True
assert snap.current_lines()[0] == "line50"
assert snap.current_lines()[-1] == "line149"
# 再上一页 → page2 = line0..49（最早一页）
assert snap.go_prev() is True
assert snap.current_lines()[0] == "line0"
assert snap.current_lines()[-1] == "line49"
# 已到最早，再翻无效
assert snap.go_prev() is False
assert snap.current_page == 2
# 下一页（更新）回到 page1
assert snap.go_next() is True
assert snap.current_lines()[0] == "line50"
# 再下一页回到 page0（最新）
assert snap.go_next() is True
assert snap.current_lines()[-1] == "line249"
# 已在最新，再翻无效
assert snap.go_next() is False

# 空快照：1 页，无内容
empty_snap = LogSnapshot(
    lines=[], errors_only=True, since_today_6=True, fetched_at=_time.time()
)
assert empty_snap.total_pages() == 1
assert empty_snap.current_lines() == []

# 恰好 100 行 = 1 页
snap100 = LogSnapshot(
    lines=[f"x{i}" for i in range(100)],
    errors_only=False,
    since_today_6=False,
    fetched_at=_time.time(),
)
assert snap100.total_pages() == 1
assert snap100.go_prev() is False
assert snap100.go_next() is False

# logs_card 渲染：含页码、模式、刷新/翻页/切换按钮
# page0=最新一页=最后一页 → 显示「第 3/3 页」
card = MODULE.logs_card(snap, is_admin=True)
body = card["elements"][0]["text"]["content"]
assert "第 3/3 页" in body, f"应显示页码(最新=最后一页): {body.splitlines()[0]}"
assert "今日 06:00 起" in body, f"应显示模式: {body.splitlines()[0]}"
assert "共 250 行" in body
# 收集所有按钮的 action
btn_actions = []
for el in card["elements"]:
    if isinstance(el, dict) and el.get("tag") == "action":
        for b in el.get("actions", []):
            btn_actions.append(b["value"]["action"])
assert "logs_refresh" in btn_actions, f"应有刷新按钮: {btn_actions}"
assert "logs_prev" in btn_actions, f"应有上一页: {btn_actions}"
assert "logs_next" in btn_actions, f"应有下一页: {btn_actions}"
assert "logs_refresh" in btn_actions  # 切换模式也用 logs_refresh

# since_today_6=False 时按钮文案切换为「今日6点起」
card_all = MODULE.logs_card(snap100, is_admin=False)
btn_texts_all = []
for el in card_all["elements"]:
    if isinstance(el, dict) and el.get("tag") == "action":
        for b in el.get("actions", []):
            btn_texts_all.append(b["text"]["content"])
assert "今日6点起" in btn_texts_all, f"全部模式应有切回今日按钮: {btn_texts_all}"

# _today_boundary_ts：应返回今天 06:00（若当前早于 06:00 则昨天 06:00）
boundary = MODULE._today_boundary_ts()
now = _time.time()
diff = now - boundary
# 应在 [0, 86400) 内，且对应时刻的 hour=6
assert 0 <= diff < 86400, f"boundary 应在24h内: diff={diff}"
bt = _time.localtime(boundary)
assert bt.tm_hour == 6, f"boundary 应为6点: hour={bt.tm_hour}"
assert boundary <= now, "boundary 不应晚于当前"

print("Feishu operations bot checks: OK")
