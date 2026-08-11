"""舞萌开字母看板逻辑单测（不依赖 nonebot/曲库）。"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "libraries" / "maimaidx_guess_letter.py"
tree = ast.parse(SRC.read_text(encoding="utf-8"))

names = {
    "BOARD_SIZE",
    "_LATIN_RE",
    "_CJK_RE",
    "_is_maskable",
    "_norm_token",
    "_title_maskable_count",
    "_latin_letter_count",
    "WEIGHT_LETTER_HIT",
    "WEIGHT_LETTER_COMPLETE",
    "WEIGHT_SONG_OPEN",
    "STAR_THRESHOLDS",
    "SCORE_POOL_BY_STAR",
    "format_elapsed",
    "format_elapsed_diff_suffix",
    "format_finish_elapsed_line",
    "combo_solved_count",
    "format_combo_tip",
    "default_star_limits",
    "star_for_elapsed",
    "star_text",
    "star_text_draw",
    "stars_for_draw",
    "format_threshold_lines",
    "distribute_pool",
    "LetterContribution",
    "LetterPlayerReward",
    "LetterSettlement",
    "LetterSong",
    "LetterBoard",
    "LetterGuessManager",
    "_format_letter_complete",
    "format_settlement_message",
    "format_board_text",
    "format_settlement_ranking_text",
    "format_settlement_text",
    "TEXT_MODE_MIN_CONTRIBUTORS",
    "TEXT_MODE_BURST_WINDOW",
    "TEXT_MODE_BURST_COUNT",
    "LETTER_ANSWER_COOLDOWN_SECONDS",
    "LETTER_TRIPLE_START",
    "LETTER_TRIPLE_DURATION",
    "LETTER_TRIPLE_MULTIPLIER",
    "letter_reward_multiplier",
    "letter_triple_active",
    "letter_triple_banner",
}
selected = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
        if isinstance(node, ast.ClassDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    selected.append(node)
                    break
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in names:
            selected.append(node)

ns = {
    "re": re,
    "time": time,
    "dataclass": dataclass,
    "field": field,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
    "Set": Set,
    "Tuple": Tuple,
    "Union": Union,
    "match_guess_answer": lambda text, answers, *, allow_latin_typo=True: any(
        str(text).strip().lower() in re.sub(r"[^a-z0-9]", "", str(a).lower())
        for a in answers
    ),
    "format_guess_answer_rate_limit": lambda remain: (
        f"嘿嘿，你的答案被我吃掉啦！({remain:.1f}秒后才能发送新的答案）"
    ),
}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SRC), "exec"), ns)

LetterSong = ns["LetterSong"]
LetterBoard = ns["LetterBoard"]
LetterGuessManager = ns["LetterGuessManager"]
_format_letter_complete = ns["_format_letter_complete"]
_is_maskable = ns["_is_maskable"]
_norm_token = ns["_norm_token"]
format_elapsed = ns["format_elapsed"]
format_elapsed_diff_suffix = ns["format_elapsed_diff_suffix"]
format_finish_elapsed_line = ns["format_finish_elapsed_line"]
combo_solved_count = ns["combo_solved_count"]
format_combo_tip = ns["format_combo_tip"]
default_star_limits = ns["default_star_limits"]
star_for_elapsed = ns["star_for_elapsed"]
star_text = ns["star_text"]
format_threshold_lines = ns["format_threshold_lines"]
distribute_pool = ns["distribute_pool"]
format_settlement_message = ns["format_settlement_message"]
format_board_text = ns["format_board_text"]
format_settlement_ranking_text = ns["format_settlement_ranking_text"]
format_settlement_text = ns["format_settlement_text"]
TEXT_MODE_MIN_CONTRIBUTORS = ns["TEXT_MODE_MIN_CONTRIBUTORS"]
TEXT_MODE_BURST_WINDOW = ns["TEXT_MODE_BURST_WINDOW"]
TEXT_MODE_BURST_COUNT = ns["TEXT_MODE_BURST_COUNT"]
LETTER_ANSWER_COOLDOWN_SECONDS = ns["LETTER_ANSWER_COOLDOWN_SECONDS"]
LETTER_TRIPLE_START = ns["LETTER_TRIPLE_START"]
LETTER_TRIPLE_DURATION = ns["LETTER_TRIPLE_DURATION"]
LETTER_TRIPLE_MULTIPLIER = ns["LETTER_TRIPLE_MULTIPLIER"]
letter_reward_multiplier = ns["letter_reward_multiplier"]
letter_triple_active = ns["letter_triple_active"]
letter_triple_banner = ns["letter_triple_banner"]
SCORE_POOL_BY_STAR = ns["SCORE_POOL_BY_STAR"]

# Combo：开字母补齐 ≥2；开歌 = 1 + 附带补齐
assert combo_solved_count(completed=0) == 0
assert combo_solved_count(completed=1) == 1
assert combo_solved_count(completed=2) == 2
assert combo_solved_count(completed=0, song_opened=True) == 1
assert combo_solved_count(completed=2, song_opened=True) == 3
assert format_combo_tip(1) == ""
assert format_combo_tip(2) == "Combo! ×2"
assert format_combo_tip(3) == "Combo! ×3"

# 相对上一局用时 diff
assert format_elapsed_diff_suffix(40.0, None) == ""
assert format_elapsed_diff_suffix(39.829, 45.0) == " (-5.171秒)"
assert format_elapsed_diff_suffix(50.0, 40.0) == " (+10.000秒)"
assert format_elapsed_diff_suffix(42.0, 42.0) == " (+0.000秒)"
assert format_finish_elapsed_line(39.829, None) == "🎉 本游戏已结束，时间: 39.829秒"
assert (
    format_finish_elapsed_line(39.829, 45.0)
    == "🎉 本游戏已结束，时间: 39.829秒 (-5.171秒)"
)
song = LetterSong(
    music_id="1",
    title="Halcyon",
    answers=["Halcyon", "halcyon", "1"],
)
board = LetterBoard(songs=[song])
assert song.display(board.revealed) == "???????"
msg_key = _norm_token("Y")
board.revealed.add(msg_key)
board.opened_order.append(msg_key)
assert song.display(board.revealed) == "????y??"
for ch in "halcon":
    board.revealed.add(ch)
assert song.is_fully_revealed(board.revealed)
newly = board.claim_fully_revealed("补齐侠")
assert newly and newly[0].solved and newly[0].solved_by == "补齐侠"

assert _is_maskable("m")
assert not _is_maskable(" ")
assert "HOT LIMIT" in _format_letter_complete(
    [LetterSong("9", "HOT LIMIT", ["HOT LIMIT"], solved=True, solved_by="x")]
)

mgr = LetterGuessManager()
song_a = LetterSong(music_id="a", title="MIRROR", answers=["MIRROR", "mirror"])
song_b = LetterSong(music_id="b", title="HOT LIMIT", answers=["HOT LIMIT"])
stuck_board = LetterBoard(
    songs=[song_a, song_b],
    revealed={"h", "o", "t", "l", "i"},
)
mgr.Group[1] = stuck_board
msg, out_board, hit, completed, hidden_before = mgr.open_song(
    1, "MIRROR", solver="开歌侠", uid="u1", billing_id=10001
)
assert hit is song_a and hit.solved and hit.solved_by == "开歌侠"
assert completed and completed[0] is song_b
assert song_b.solved and song_b.solved_by == "开歌侠"
assert "字母补齐：HOT LIMIT" in msg
assert hidden_before["b"] == 1
assert "m" in out_board.revealed
assert out_board.finished
assert out_board.contributions["u1"].song_opens == 1
assert out_board.contributions["u1"].letter_completes == 1

# 开字母必须关闭三字符的一处错字容忍：输入 the 不能误开只有 she/tie 的歌。
mgr_strict = LetterGuessManager()
false_hit = LetterSong(music_id="the-1", title="SHE LOVES YOU", answers=["SHE LOVES YOU", "tie"])
real_hit = LetterSong(music_id="the-2", title="THE BRIGHT SIDE", answers=["THE BRIGHT SIDE"])
strict_board = LetterBoard(songs=[false_hit, real_hit])
mgr_strict.Group[4] = strict_board
_, _, strict_hit, _, _ = mgr_strict.open_song(4, "the", solver="严格匹配侠")
assert strict_hit is real_hit
assert not false_hit.solved

mgr2 = LetterGuessManager()
stuck = LetterSong(music_id="c", title="AB", answers=["AB"])
hist = LetterBoard(songs=[stuck], revealed={"a", "b"})
assert stuck.is_fully_revealed(hist.revealed) and not stuck.solved
mgr2.Group[2] = hist
msg2, _, completed2, _ = mgr2.open_letter(
    2, "a", solver="补洞侠", uid="u2", billing_id=10002
)
assert completed2 and completed2[0].solved_by == "补洞侠"
assert "字母补齐：AB" in msg2
assert "已经开过" not in msg2
assert hist.contributions["u2"].letter_completes == 1

# 已开过且无补齐：静默（空文案）
mgr3 = LetterGuessManager()
dup = LetterSong(music_id="d", title="AB", answers=["AB"], solved=True, solved_by="x")
dup_board = LetterBoard(songs=[dup], revealed={"a", "b"})
mgr3.Group[3] = dup_board
msg3, _, completed3, _ = mgr3.open_letter(3, "a", solver="刷屏侠", uid="u3")
assert msg3 == "" and completed3 == []

assert format_elapsed(12.3456) == "12.346秒"
assert format_elapsed(0) == "0.000秒"
assert star_for_elapsed(30.0) == 5
assert star_for_elapsed(30.001) == 4
assert star_for_elapsed(45.0) == 4
assert star_for_elapsed(60.0) == 3
assert star_for_elapsed(90.0) == 2
assert star_for_elapsed(180.0) == 1
assert star_for_elapsed(180.001) == 0
assert "⭐️⭐️⭐️" == star_text(3)
assert "超时" in star_text(0)
assert "★★★" == ns["star_text_draw"](3)
assert "☆" in ns["star_text_draw"](0)
assert "★★★" == ns["stars_for_draw"]("⭐️⭐️⭐️")

# 自定义自适应阈值：五星 20s → 四星 30s
adaptive = {5: 20.0, 4: 30.0, 3: 40.0, 2: 60.0, 1: 120.0}
assert star_for_elapsed(20.0, adaptive) == 5
assert star_for_elapsed(20.001, adaptive) == 4
assert star_for_elapsed(120.001, adaptive) == 0
assert "20.000秒" in format_threshold_lines(adaptive, adaptive=True, sample_count=12)

dist = distribute_pool({"a": 3, "b": 1, "c": 0}, 10)
assert dist["a"] + dist["b"] + dist["c"] == 10
assert dist["a"] == 8 and dist["b"] == 2 and dist["c"] == 0
assert distribute_pool({"a": 0}, 5) == {"a": 0}

settle_board = LetterBoard(
    songs=[
        LetterSong("1", "AA", ["AA"], solved=True, solved_by="甲"),
        LetterSong("2", "BB", ["BB"], solved=True, solved_by="乙"),
    ],
    started_at=1000.0,
)
settle_board.ensure_contribution("1", 11, "甲").letter_hits = 2
settle_board.ensure_contribution("1", 11, "甲").song_opens = 1
settle_board.ensure_contribution("2", 22, "乙").letter_completes = 1
# event_now 取活动前，保证基线 1 倍结算可断言
result = settle_board.settle(now=1025.5, event_now=LETTER_TRIPLE_START - 1)
assert abs(result.elapsed - 25.5) < 1e-9
assert result.stars == 5
assert result.reward_multiplier == 1
assert result.score_pool == SCORE_POOL_BY_STAR[5]
# 甲 song_opens=1、乙 letter_completes=1，各完整开出1首并列最多 → 各得2，总池4
assert result.break_pool == 4
assert sum(r.score for r in result.rewards) == result.score_pool
assert sum(r.break_points for r in result.rewards) == result.break_pool
text = format_settlement_message(result)
assert "25.500秒" in text
assert "⭐️⭐️⭐️⭐️⭐️" in text
assert "本局奖池：40 分 / 4 BREAK" in text
assert "按贡献分配" in text
assert "限时×" not in text
assert "本局阈值" not in text  # 阈值放分成图，短文案不含

# 限时×3：积分池与 BREAK 均乘倍，文案标注
triple = settle_board.settle(
    now=1025.5, event_now=LETTER_TRIPLE_START + 60
)
assert triple.reward_multiplier == LETTER_TRIPLE_MULTIPLIER
assert triple.score_pool == SCORE_POOL_BY_STAR[5] * LETTER_TRIPLE_MULTIPLIER
assert triple.break_pool == 4 * LETTER_TRIPLE_MULTIPLIER
assert sum(r.score for r in triple.rewards) == triple.score_pool
assert sum(r.break_points for r in triple.rewards) == triple.break_pool
triple_text = format_settlement_message(triple)
assert f"限时×{LETTER_TRIPLE_MULTIPLIER}" in triple_text
assert "120 分 / 12 BREAK" in triple_text
assert letter_triple_active(now=LETTER_TRIPLE_START)
assert not letter_triple_active(now=LETTER_TRIPLE_START + LETTER_TRIPLE_DURATION)
assert "限时×" in letter_triple_banner(now=LETTER_TRIPLE_START + 1)
assert letter_triple_banner(now=LETTER_TRIPLE_START - 1) == ""
assert letter_reward_multiplier(now=LETTER_TRIPLE_START - 1) == 1

# 停表：通关后 freeze_end 定格，后续墙钟不再计入用时
freeze_board = LetterBoard(
    songs=[
        LetterSong("1", "AA", ["AA"], solved=True, solved_by="甲"),
        LetterSong("2", "BB", ["BB"], solved=True, solved_by="乙"),
    ],
    started_at=1000.0,
)
assert freeze_board.freeze_end(now=1025.5) == 25.5
assert freeze_board.ended_at == 1025.5
# 再次 freeze / 更晚的 now 不影响已定格用时
assert freeze_board.freeze_end(now=9999.0) == 25.5
assert abs(freeze_board.elapsed(now=9999.0) - 25.5) < 1e-9
freeze_board.ensure_contribution("1", 11, "甲").letter_hits = 1
frozen = freeze_board.settle(now=9999.0, event_now=LETTER_TRIPLE_START - 1)
assert abs(frozen.elapsed - 25.5) < 1e-9
assert frozen.stars == 5
assert "25.500秒" in frozen.elapsed_text

# 文字看板 / 文字结算榜
board_txt = format_board_text(settle_board)
assert "【舞萌开字母】进度 2/2" in board_txt
assert "✅ AA" in board_txt
assert "✅ BB" in board_txt
assert "🤔" in format_board_text(
    LetterBoard(songs=[LetterSong("9", "XY", ["XY"])], revealed=set())
)
rank_txt = format_settlement_ranking_text(result)
assert "#1" in rank_txt and "权重" in rank_txt
assert "→ +" in rank_txt and "BREAK" in rank_txt
assert "开字母×" in rank_txt or "开歌×" in rank_txt or "补齐×" in rank_txt
full_txt = format_settlement_text(result, settle_board)
assert "全部解开" in full_txt and "【舞萌开字母】" in full_txt
assert f"限时×{LETTER_TRIPLE_MULTIPLIER}" in format_settlement_ranking_text(triple)

# 文字模式：贡献人数阈值（粘性）
assert TEXT_MODE_MIN_CONTRIBUTORS == 3
assert TEXT_MODE_BURST_WINDOW == 2.0
assert TEXT_MODE_BURST_COUNT == 4
crowd = LetterBoard(
    songs=[LetterSong("1", "AA", ["AA"], solved=True, solved_by="x")],
    started_at=0.0,
)
for i in range(TEXT_MODE_MIN_CONTRIBUTORS):
    crowd.ensure_contribution(str(i), 1000 + i, f"p{i}").letter_hits = 1
assert crowd.contributor_count == TEXT_MODE_MIN_CONTRIBUTORS
assert not crowd.text_mode
assert crowd.prefer_text() and crowd.text_mode  # 粘性锁定

# 文字模式：突发处理次数
burst = LetterBoard(
    songs=[LetterSong("1", "AA", ["AA"])],
    started_at=time.time(),
)
t0 = time.time()
for i in range(TEXT_MODE_BURST_COUNT):
    burst.note_process(now=t0 + i * 0.01)
assert burst.text_mode
assert burst.prefer_text()

# 开字母专用 2.5s 冷却：非高峰提示一次后静默；高峰跳过
assert LETTER_ANSWER_COOLDOWN_SECONDS == 2.5
cd_board = LetterBoard(songs=[LetterSong("1", "AA", ["AA"])], started_at=t0)
assert cd_board.try_consume_answer("u1", now=t0) is None
tip = cd_board.try_consume_answer("u1", now=t0 + 0.5)
assert tip == "嘿嘿，你的答案被我吃掉啦！(2.0秒后才能发送新的答案）"
assert cd_board.try_consume_answer("u1", now=t0 + 1.0) == ""  # 静默
assert cd_board.try_consume_answer("u1", now=t0 + LETTER_ANSWER_COOLDOWN_SECONDS) is None
# 高峰文字模式不检查
peak = LetterBoard(songs=[LetterSong("1", "AA", ["AA"])], text_mode=True)
assert peak.try_consume_answer("u9", now=t0) is None
assert peak.try_consume_answer("u9", now=t0 + 0.1) is None

slow = LetterBoard(
    songs=[LetterSong("1", "AA", ["AA"], solved=True, solved_by="甲")],
    started_at=0.0,
)
slow.ensure_contribution("1", 11, "甲").letter_hits = 1
slow_result = slow.settle(now=200.0, event_now=LETTER_TRIPLE_START - 1)
assert slow_result.stars == 0
assert slow_result.score_pool == SCORE_POOL_BY_STAR[0]

# 自适应阈值单测（独立模块，直接 import 源码 AST）
stats_src = ROOT / "libraries" / "maimaidx_letter_stats.py"
stats_tree = ast.parse(stats_src.read_text(encoding="utf-8"))
stats_names = {
    "DEFAULT_FIVE_STAR",
    "MIN_FIVE_STAR",
    "MAX_FIVE_STAR",
    "STAR_RATIO",
    "HISTORY_WINDOW",
    "MIN_SAMPLES_FOR_ADAPTIVE",
    "DAILY_LOOKBACK_DAYS",
    "MIN_GOAL_CLEARS",
    "MAX_GOAL_CLEARS",
    "DEFAULT_GOAL_CLEARS",
    "MIN_GOAL_WEIGHT",
    "MAX_GOAL_WEIGHT",
    "DEFAULT_GOAL_WEIGHT",
    "MIN_SAMPLES_FOR_FASTEST_GOAL",
    "StarThresholds",
    "DailyGoalsSpec",
    "default_thresholds",
    "_percentile",
    "compute_thresholds",
    "day_key",
    "compute_daily_goals",
    "_day_start_ts",
    "is_better_record",
}
stats_selected = []
for node in stats_tree.body:
    if isinstance(node, ast.ClassDef) and node.name in stats_names:
        stats_selected.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in stats_names:
        stats_selected.append(node)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in stats_names:
                stats_selected.append(node)
                break
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in stats_names:
        stats_selected.append(node)

stats_ns = {
    "dataclass": dataclass,
    "field": field,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
    "Tuple": Tuple,
    "Union": Union,
    "frozen": True,
    "math": __import__("math"),
    "time": time,
}
# StarThresholds / DailyGoalsSpec use @dataclass(frozen=True)
import dataclasses

stats_ns["dataclass"] = dataclasses.dataclass
exec(compile(ast.Module(body=stats_selected, type_ignores=[]), str(stats_src), "exec"), stats_ns)
compute_thresholds = stats_ns["compute_thresholds"]
default_thresholds = stats_ns["default_thresholds"]
MIN_FIVE_STAR = stats_ns["MIN_FIVE_STAR"]
MAX_FIVE_STAR = stats_ns["MAX_FIVE_STAR"]
compute_daily_goals = stats_ns["compute_daily_goals"]
is_better_record = stats_ns["is_better_record"]
DailyGoalsSpec = stats_ns["DailyGoalsSpec"]

base = default_thresholds()
assert abs(base.limits[5] - 30.0) < 1e-9
assert abs(base.limits[1] - 180.0) < 1e-9
assert not base.adaptive

# 样本不足 → 默认
few = compute_thresholds([20.0] * 5)
assert not few.adaptive and few.limits[5] == 30.0

# 普遍很快 → 五星收紧但不低于下限
fast_hist = [18.0] * 20
fast = compute_thresholds(fast_hist)
assert fast.adaptive
assert MIN_FIVE_STAR <= fast.limits[5] <= MAX_FIVE_STAR
assert abs(fast.limits[4] / fast.limits[5] - 1.5) < 1e-9
assert abs(fast.limits[1] / fast.limits[5] - 6.0) < 1e-9

# 极端快 → 卡在 15s
ultra = compute_thresholds([8.0] * 30)
assert abs(ultra.limits[5] - MIN_FIVE_STAR) < 1e-9

# 偏慢 → 仍不超过 30s
slow_hist = compute_thresholds([50.0] * 20)
assert abs(slow_hist.limits[5] - MAX_FIVE_STAR) < 1e-9

# 破纪录比较
assert is_better_record(kind="fastest", new_value=20.0, old_value=None)
assert is_better_record(kind="fastest", new_value=19.0, old_value=20.0)
assert not is_better_record(kind="fastest", new_value=20.0, old_value=20.0)
assert not is_better_record(kind="fastest", new_value=21.0, old_value=20.0)
assert is_better_record(kind="max", new_value=10, old_value=None)
assert is_better_record(kind="max", new_value=11, old_value=10)
assert not is_better_record(kind="max", new_value=10, old_value=10)
assert not is_better_record(kind="max", new_value=0, old_value=None)

# 每日目标：无历史 → 默认
now = time.time()
empty_goals = compute_daily_goals([], now=now)
assert empty_goals.clears == stats_ns["DEFAULT_GOAL_CLEARS"]
assert empty_goals.weight == stats_ns["DEFAULT_GOAL_WEIGHT"]
assert empty_goals.fastest is None

# 近几日活跃偏高 → 通关/贡献目标上浮
day_ago = now - 86400
busy_rows = [(day_ago, 40.0, 20) for _ in range(5)] + [
    (day_ago - 86400, 35.0, 18) for _ in range(5)
]
busy_goals = compute_daily_goals(busy_rows, now=now)
assert busy_goals.clears >= empty_goals.clears
assert busy_goals.weight >= empty_goals.weight
assert "通关≥" in busy_goals.format_line()

# 样本足够 → 有最快目标
many_rows = [(now - 86400 * (i % 5 + 1), 30.0 + i, 10) for i in range(12)]
fast_goal = compute_daily_goals(many_rows, now=now)
assert fast_goal.fastest is not None

print("test_guess_letter ok")
