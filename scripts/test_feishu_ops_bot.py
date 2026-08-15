#!/usr/bin/env python3
"""Regression checks for the Feishu operations bot."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import tempfile
import threading


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
assert 'LOG_FETCH_LINES_ALL = 5000' in bot_source

# journalctl 必须有行数与执行时间硬上限，并按时间窗口过滤。
controller = MODULE.SystemController()
run_calls = []
controller._run = lambda args, timeout=15: run_calls.append((args, timeout)) or ""
assert controller.logs(window_secs=7200) == []
assert "-n" in run_calls[0][0]
assert str(MODULE.LOG_FETCH_LINES_ALL) in run_calls[0][0]
assert "--since" in run_calls[0][0]
assert run_calls[0][1] == MODULE.LOG_FETCH_TIMEOUT_SECONDS

# ── 后台日志刷新：先发占位卡，worker 完成后发新卡片并撤回占位卡 ──
queued_bot = object.__new__(MODULE.FeishuOpsBot)
queued_bot._log_lock = threading.Lock()
queued_bot._log_refreshing = set()
queued_bot._log_snapshots = {}
refresh_started = threading.Event()
allow_finish = threading.Event()
delivered = threading.Event()
deliveries = []
recalled = threading.Event()
recall_ids = []

def fake_refresh(receive_id, errors_only, window_secs, is_admin):
    refresh_started.set()
    assert allow_finish.wait(2)
    return MODULE._card("刷新完成", "ok")

def fake_send(receive_id_type, receive_id, card):
    deliveries.append((receive_id_type, receive_id, card))
    # 第一次是占位卡，第二次是结果卡片
    if len(deliveries) == 2:
        delivered.set()
    return f"om_{len(deliveries)}"

def fake_delete(message_id):
    recall_ids.append(message_id)
    recalled.set()

queued_bot._refresh_logs = fake_refresh
queued_bot._send_card = fake_send
queued_bot._delete_card = fake_delete
# 正常提交返回 None（不再返回占位卡）
pending = queued_bot._queue_log_refresh("chat_id", "oc_test", False, 300, True)
assert pending is None, "正常提交应返回 None（占位卡已自行发送）"
# 占位卡已发送（第一张）
assert len(deliveries) == 1, "应先发占位卡"
assert deliveries[0][2]["header"]["title"]["content"] == "日志刷新中"
assert "近 5m" in deliveries[0][2]["elements"][0]["text"]["content"]
assert refresh_started.wait(1)
assert not delivered.is_set(), "worker 应等待 allow_finish"
allow_finish.set()
assert delivered.wait(2), "worker 应发送结果卡片"
# 结果卡片已发送（第二张）
assert deliveries[1][2]["header"]["title"]["content"] == "刷新完成"
assert deliveries[1][0:2] == ("chat_id", "oc_test")
# 占位卡已被撤回
assert recalled.wait(2), "应撤回占位卡"
assert recall_ids[0] == "om_1", f"应撤回占位卡 id, got {recall_ids[0]}"
assert not queued_bot._log_refreshing, "完成后应清空 refreshing set"

# 已有同类刷新任务在跑时，重复提交返回「刷新中」占位卡，不启动新 worker。
queued_bot._log_refreshing.add(("oc_dup", False))
dup = queued_bot._queue_log_refresh("chat_id", "oc_dup", False, 300, True)
assert dup["header"]["title"]["content"] == "日志刷新中"
assert len(deliveries) == 2, "重复提交不应再投递新卡片"

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

# 250 行 / 50 每页 → 5 页，page 0 = 最新一页 = 末尾 50 行 (line200..249)
snap = LogSnapshot(
    lines=[f"line{i}" for i in range(250)],
    errors_only=False,
    fetched_at=_time.time(),
    window_secs=7200,
)
assert snap.total_pages() == 5, f"250行应5页: {snap.total_pages()}"
assert snap.current_lines()[0] == "line200", f"page0应从line200开始: {snap.current_lines()[0]}"
assert snap.current_lines()[-1] == "line249"
# 上一页（更早）→ page1 = line150..199
assert snap.go_prev() is True
assert snap.current_lines()[0] == "line150"
assert snap.current_lines()[-1] == "line199"
# 连续翻到最早一页 page4 = line0..49
assert snap.go_prev() is True  # page2
assert snap.go_prev() is True  # page3
assert snap.go_prev() is True  # page4
assert snap.current_lines()[0] == "line0"
assert snap.current_lines()[-1] == "line49"
# 已到最早，再翻无效
assert snap.go_prev() is False
assert snap.current_page == 4
# 下一页（更新）回到 page3
assert snap.go_next() is True
assert snap.current_lines()[0] == "line50"
# 连续翻回最新 page0
assert snap.go_next() is True  # page2
assert snap.go_next() is True  # page1
assert snap.go_next() is True  # page0
assert snap.current_lines()[-1] == "line249"
# 已在最新，再翻无效
assert snap.go_next() is False

# 空快照：1 页，无内容
empty_snap = LogSnapshot(
    lines=[], errors_only=True, fetched_at=_time.time(), window_secs=3600
)
assert empty_snap.total_pages() == 1
assert empty_snap.current_lines() == []

# 恰好 50 行 = 1 页
snap100 = LogSnapshot(
    lines=[f"x{i}" for i in range(50)],
    errors_only=False,
    fetched_at=_time.time(),
    window_secs=600,
)
assert snap100.total_pages() == 1
assert snap100.go_prev() is False
assert snap100.go_next() is False

# logs_card 渲染：含页码、窗口标签、刷新/翻页/窗口切换按钮
# page0=最新一页=最后一页 → 显示「第 5/5 页」
card = MODULE.logs_card(snap, is_admin=True)
body = card["elements"][0]["text"]["content"]
assert "第 5/5 页" in body, f"应显示页码(最新=最后一页): {body.splitlines()[0]}"
assert "近 2h" in body, f"应显示窗口标签: {body.splitlines()[0]}"
assert "共 250 行" in body
# 收集所有按钮的 action 与文案
btn_actions = []
btn_texts = []
for el in card["elements"]:
    if isinstance(el, dict) and el.get("tag") == "action":
        for b in el.get("actions", []):
            btn_actions.append(b["value"]["action"])
            btn_texts.append(b["text"]["content"])
assert "logs_refresh" in btn_actions, f"应有刷新按钮: {btn_actions}"
assert "logs_prev" in btn_actions, f"应有上一页: {btn_actions}"
assert "logs_next" in btn_actions, f"应有下一页: {btn_actions}"
# 当前窗口 7200(2h) 时，应提供切到 1h/10m 的快捷按钮（不含 2h 自身）
assert "近1h" in btn_texts, f"应有近1h切换: {btn_texts}"
assert "近10m" in btn_texts, f"应有近10m切换: {btn_texts}"
assert "近2h" not in btn_texts, f"不应出现当前窗口自身: {btn_texts}"

# window_secs=600 时标签为「近 10m」，并提供切到 1h/2h 的按钮
card_all = MODULE.logs_card(snap100, is_admin=False)
body_all = card_all["elements"][0]["text"]["content"]
assert "近 10m" in body_all, f"10m窗口应显示近10m: {body_all.splitlines()[0]}"
btn_texts_all = []
for el in card_all["elements"]:
    if isinstance(el, dict) and el.get("tag") == "action":
        for b in el.get("actions", []):
            btn_texts_all.append(b["text"]["content"])
assert "近1h" in btn_texts_all, f"10m窗口应有近1h切换: {btn_texts_all}"
assert "近2h" in btn_texts_all, f"10m窗口应有近2h切换: {btn_texts_all}"
assert "近10m" not in btn_texts_all, f"不应出现当前窗口自身: {btn_texts_all}"

# parse_window_secs：仅允许 m/h，非法返回 None
assert MODULE.parse_window_secs("2h") == 7200
assert MODULE.parse_window_secs("1h") == 3600
assert MODULE.parse_window_secs("10m") == 600
assert MODULE.parse_window_secs("30m") == 1800
assert MODULE.parse_window_secs("  3H ") == 10800  # 大小写/空白容错
assert MODULE.parse_window_secs("") is None
assert MODULE.parse_window_secs("5s") is None  # 不支持秒
assert MODULE.parse_window_secs("abc") is None
assert MODULE.parse_window_secs("0h") is None  # 0 不合法
assert MODULE.parse_window_secs("-1h") is None

# ── 去头填尾增量刷新 ──
# 场景1：有快照且未过期 → 增量拉取，新行追加到尾部，超限丢头部旧行
incr_bot = object.__new__(MODULE.FeishuOpsBot)
incr_bot._log_lock = threading.Lock()
incr_bot._log_snapshots = {}
# 预置快照：已存满 LOG_FETCH_LINES_ALL 行，最后一行时间戳为 2026-01-01 00:00:00
full_lines = [
    f"2026-01-01 00:00:{i:02d} INFO old line {i}"
    for i in range(MODULE.LOG_FETCH_LINES_ALL)
]
last_ts_old = "2026-01-01 00:00:00"
incr_bot._log_snapshots[("oc_inc", False)] = MODULE.LogSnapshot(
    lines=list(full_lines),
    errors_only=False,
    fetched_at=_time.time(),  # 刚刚拉取，未过期
    window_secs=7200,
    last_ts=last_ts_old,
)
incr_calls = []

class FakeSystem:
    def logs_incremental(self, *, errors_only=False, since_ts=""):
        incr_calls.append(since_ts)
        # 模拟新增 3 行（时间戳都晚于 since_ts）
        return [
            "2026-01-01 00:00:01 INFO new line A",
            "2026-01-01 00:00:02 INFO new line B",
            "2026-01-01 00:00:03 INFO new line C",
        ]
    def logs(self, *, errors_only=False, window_secs=7200):
        raise AssertionError("增量可用时不应走全量 fallback")

incr_bot.system = FakeSystem()
card = incr_bot._refresh_logs("oc_inc", False, 7200, True)
assert incr_calls == [last_ts_old], f"应基于 last_ts 增量拉取: {incr_calls}"
snap_after = incr_bot._log_snapshots[("oc_inc", False)]
# 去头填尾：总行数不超 LOG_FETCH_LINES_ALL，尾部为新行
assert len(snap_after.lines) == MODULE.LOG_FETCH_LINES_ALL
assert snap_after.lines[-3:] == [
    "2026-01-01 00:00:01 INFO new line A",
    "2026-01-01 00:00:02 INFO new line B",
    "2026-01-01 00:00:03 INFO new line C",
], "尾部应为新增行"
# 头部 3 行被丢弃（原 line0/1/2 不再存在）
assert "old line 0" not in snap_after.lines[0]
assert snap_after.last_ts == "2026-01-01 00:00:03"
assert snap_after.current_page == 0  # 回到最新页
body = card["elements"][0]["text"]["content"]
assert "new line C" in body

# 场景2：无快照 → 全量拉取，创建新快照
fresh_bot = object.__new__(MODULE.FeishuOpsBot)
fresh_bot._log_lock = threading.Lock()
fresh_bot._log_snapshots = {}
full_calls = []

class FakeSystemFull:
    def logs_incremental(self, *, errors_only=False, since_ts=""):
        raise AssertionError("无快照不应走增量")
    def logs(self, *, errors_only=False, window_secs=7200):
        full_calls.append(window_secs)
        return ["2026-01-02 00:00:00 INFO full line 1"]

fresh_bot.system = FakeSystemFull()
card2 = fresh_bot._refresh_logs("oc_fresh", False, 3600, True)
assert full_calls == [3600], f"应按 window_secs 全量拉取: {full_calls}"
snap2 = fresh_bot._log_snapshots[("oc_fresh", False)]
assert snap2.lines == ["2026-01-02 00:00:00 INFO full line 1"]
assert snap2.window_secs == 3600
assert snap2.last_ts == "2026-01-02 00:00:00"

# 场景3：增量失败 → fallback 全量
fb_bot = object.__new__(MODULE.FeishuOpsBot)
fb_bot._log_lock = threading.Lock()
fb_bot._log_snapshots = {}
fb_bot._log_snapshots[("oc_fb", False)] = MODULE.LogSnapshot(
    lines=["2026-01-01 00:00:00 INFO old"],
    errors_only=False,
    fetched_at=_time.time(),
    window_secs=7200,
    last_ts="2026-01-01 00:00:00",
)
fb_calls = {"incr": 0, "full": 0}

class FakeSystemFail:
    def logs_incremental(self, *, errors_only=False, since_ts=""):
        fb_calls["incr"] += 1
        raise RuntimeError("journalctl timeout")
    def logs(self, *, errors_only=False, window_secs=7200):
        fb_calls["full"] += 1
        return ["2026-01-01 00:00:05 INFO fallback line"]

fb_bot.system = FakeSystemFail()
card3 = fb_bot._refresh_logs("oc_fb", False, 7200, True)
assert fb_calls["incr"] == 1 and fb_calls["full"] == 1, "增量失败应 fallback 全量"
snap3 = fb_bot._log_snapshots[("oc_fb", False)]
assert snap3.lines == ["2026-01-01 00:00:05 INFO fallback line"], "fallback 后应覆盖旧快照"

# 场景4：快照过期（超过 LOG_SNAPSHOT_STALE_SECS）→ 走全量
stale_bot = object.__new__(MODULE.FeishuOpsBot)
stale_bot._log_lock = threading.Lock()
stale_bot._log_snapshots = {}
stale_bot._log_snapshots[("oc_stale", False)] = MODULE.LogSnapshot(
    lines=["2026-01-01 00:00:00 INFO stale"],
    errors_only=False,
    fetched_at=_time.time() - MODULE.LOG_SNAPSHOT_STALE_SECS - 1,  # 已过期
    window_secs=7200,
    last_ts="2026-01-01 00:00:00",
)
stale_calls = {"incr": 0, "full": 0}

class FakeSystemStale:
    def logs_incremental(self, *, errors_only=False, since_ts=""):
        stale_calls["incr"] += 1
        return []
    def logs(self, *, errors_only=False, window_secs=7200):
        stale_calls["full"] += 1
        return ["2026-01-01 00:00:10 INFO fresh full"]

stale_bot.system = FakeSystemStale()
stale_bot._refresh_logs("oc_stale", False, 7200, True)
assert stale_calls["incr"] == 0, "过期快照不应走增量"
assert stale_calls["full"] == 1, "过期应走全量"

# 场景5：窗口变更 → 走全量（不能复用旧窗口的增量）
wchg_bot = object.__new__(MODULE.FeishuOpsBot)
wchg_bot._log_lock = threading.Lock()
wchg_bot._log_snapshots = {}
wchg_bot._log_snapshots[("oc_wchg", False)] = MODULE.LogSnapshot(
    lines=["2026-01-01 00:00:00 INFO old"],
    errors_only=False,
    fetched_at=_time.time(),
    window_secs=7200,  # 旧窗口 2h
    last_ts="2026-01-01 00:00:00",
)
wchg_calls = {"incr": 0, "full": 0}

class FakeSystemWchg:
    def logs_incremental(self, *, errors_only=False, since_ts=""):
        wchg_calls["incr"] += 1
        return []
    def logs(self, *, errors_only=False, window_secs=7200):
        wchg_calls["full"] += 1
        return ["2026-01-01 00:00:20 INFO 1h window"]

wchg_bot.system = FakeSystemWchg()
# 请求 1h（3600）≠ 快照 2h（7200）→ 不能增量
assert wchg_bot._can_incremental(
    wchg_bot._log_snapshots[("oc_wchg", False)], 3600
) is False
wchg_bot._refresh_logs("oc_wchg", False, 3600, True)
assert wchg_calls["incr"] == 0, "窗口变更不应走增量"
assert wchg_calls["full"] == 1

print("Feishu operations bot checks: OK")
