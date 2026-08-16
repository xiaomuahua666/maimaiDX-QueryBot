"""舞萌开字母：多曲标题 hangman 看板。

交互对齐常见 Bot：
- 开字母 / 舞萌开字母：开局（8 曲）
- 开字母 x：揭示字母/字符
- 开歌 <曲名|别名>：猜中整首歌
- 不玩了 / 结束开字母：结束并揭晓

计分规则（通关结算）：
- 局内开字母 / 补齐 / 开歌只记贡献，不即时发积分或 BREAK
- 全部解开后按用时星级给全员奖池，再按贡献权重分配
- 「不玩了」只揭晓，不发速度奖与贡献奖
- 默认星级：≤30/45/60/90/180 秒；样本足够后按群历史 P35 自适应收紧
  （五星上限夹在 15–30 秒，其它星按 1:1.5:2:3:6 缩放）
- 超出一星上限仍结算，为 0 星最低奖池；无贡献者不得分
- 用时按浮点秒计算，展示统一为三位小数（如 42.318秒）
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from PIL import Image, ImageDraw

from ..config import SIYUAN, TBFONT, footer_generated, log
from .image import DrawText, image_to_base64
from .maimaidx_guess_match import match_guess_answer
from .maimaidx_guess_rate_limit import format_guess_answer_rate_limit
from .maimaidx_music import guess, mai
from .maimaidx_model import Music

GroupId = Union[int, str]

BOARD_SIZE = 8
_LATIN_RE = re.compile(r"[A-Za-z0-9]")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 贡献权重：有效开字母 / 字母补齐曲 / 主动开歌
WEIGHT_LETTER_HIT = 1
WEIGHT_LETTER_COMPLETE = 3
WEIGHT_SONG_OPEN = 4

# 用时（秒）→ 星级；超出最高阈值则为 0 星（浮点比较）
STAR_THRESHOLDS: Tuple[Tuple[float, int], ...] = (
    (30.0, 5),
    (45.0, 4),
    (60.0, 3),
    (90.0, 2),
    (180.0, 1),
)

# 星级 → 全员积分池（再按贡献分配；明显弱于「每开一次就涨」）
SCORE_POOL_BY_STAR: Dict[int, int] = {5: 40, 4: 28, 3: 20, 2: 12, 1: 8, 0: 4}
# BREAK 不再走星级奖池：每个开出完整曲目(song_opens≥1)者得 1，
# 开歌数量最多者(含并列)得 2。限时倍率仍可放大。

# 开字母限时活动：结算分数 / BREAK ×3（仅开字母；过期自动恢复 1 倍）
# 自 2026-07-19 23:30 CST（≈ 1784475000）起持续 7 天
LETTER_TRIPLE_START = 1784475000
LETTER_TRIPLE_DURATION = 7 * 86400
LETTER_TRIPLE_MULTIPLIER = 3

# 人多/突发负载 → 文字模式（本局粘性；局中看板可走文字，通关结算仍强制出图）
TEXT_MODE_MIN_CONTRIBUTORS = 3  # 本局有贡献的 uid 数 ≥ 此值
TEXT_MODE_BURST_WINDOW = 2.0  # 秒：统计窗口
TEXT_MODE_BURST_COUNT = 4  # 窗口内开字母相关处理次数 ≥ 此值

# 开字母专用答题冷却（非高峰）；与猜歌 2.5 秒全局限频互不共用
LETTER_ANSWER_COOLDOWN_SECONDS = 2.5


def letter_reward_multiplier(*, now: Optional[float] = None) -> int:
    """开字母结算倍率：限时活动内为 LETTER_TRIPLE_MULTIPLIER，否则 1。"""
    t = time.time() if now is None else float(now)
    end = LETTER_TRIPLE_START + LETTER_TRIPLE_DURATION
    if LETTER_TRIPLE_START <= t < end:
        return LETTER_TRIPLE_MULTIPLIER
    return 1


def letter_triple_active(*, now: Optional[float] = None) -> bool:
    return letter_reward_multiplier(now=now) > 1


def letter_triple_banner(*, now: Optional[float] = None) -> str:
    """活动提示文案；未在活动期内返回空串。"""
    if not letter_triple_active(now=now):
        return ""
    return f"🔥 限时×{LETTER_TRIPLE_MULTIPLIER}：本周开字母结算分数与 BREAK 三倍！"

_BG = (42, 28, 22, 255)
_CARD = (58, 40, 32, 255)
_TITLE = (255, 214, 170, 255)
_OK = (120, 210, 140, 255)
_WAIT = (255, 196, 120, 255)
_TEXT = (245, 236, 224, 255)
_MUTED = (180, 160, 145, 255)
_LINE = (90, 66, 54, 255)


def _is_maskable(ch: str) -> bool:
    if _LATIN_RE.fullmatch(ch) or _CJK_RE.fullmatch(ch):
        return True
    # 扩展拉丁（如 Ø）也按可开字符处理
    return len(ch) == 1 and ch.isalpha()


def _norm_token(ch: str) -> str:
    """比较用：拉丁字母小写，其余原样。"""
    if len(ch) != 1:
        return ch
    if ch.isalpha() and ch.isascii():
        return ch.lower()
    if ch.isalpha():
        return ch.casefold()
    return ch


def _title_maskable_count(title: str) -> int:
    return sum(1 for ch in title if _is_maskable(ch))


def _latin_letter_count(title: str) -> int:
    return sum(1 for ch in title if ch.isascii() and ch.isalpha())


def format_elapsed(seconds: float) -> str:
    """统一用时文案：xx.xxx秒。"""
    return f"{max(0.0, float(seconds)):.3f}秒"


def format_elapsed_diff_suffix(
    current: float, previous: Optional[float]
) -> str:
    """
    相对上一局用时差值后缀：更快为负、更慢为正。
    无上一局记录时返回空串。
    """
    if previous is None:
        return ""
    delta = float(current) - float(previous)
    sign = "+" if delta >= 0 else ""
    return f" ({sign}{delta:.3f}秒)"


def format_finish_elapsed_line(
    elapsed: float, previous: Optional[float] = None
) -> str:
    """通关先发文案：本游戏已结束 + 可选相对上一局 diff。"""
    return (
        f"🎉 本游戏已结束，时间: {format_elapsed(elapsed)}"
        f"{format_elapsed_diff_suffix(elapsed, previous)}"
    )


def combo_solved_count(*, completed: int, song_opened: bool = False) -> int:
    """一次操作解开/补齐的曲数：开字母=补齐数；开歌=1+附带补齐数。"""
    n = max(0, int(completed))
    if song_opened:
        n += 1
    return n


def format_combo_tip(count: int) -> str:
    """≥2 首时返回 Combo 提示；否则空串。"""
    n = int(count)
    if n < 2:
        return ""
    return f"Combo! ×{n}"


def default_star_limits() -> Dict[int, float]:
    """默认五档上限（秒）：30 / 45 / 60 / 90 / 180。"""
    return {5: 30.0, 4: 45.0, 3: 60.0, 2: 90.0, 1: 180.0}


def star_for_elapsed(
    elapsed: float, limits: Optional[Dict[int, float]] = None
) -> int:
    """按浮点用时取星级；超出一星上限为 0 星。"""
    t = max(0.0, float(elapsed))
    caps = limits or default_star_limits()
    for stars in (5, 4, 3, 2, 1):
        if t <= float(caps[stars]):
            return stars
    return 0


def star_text(stars: int) -> str:
    """聊天文案用星级（emoji，多数客户端可显示）。"""
    n = max(0, min(5, int(stars)))
    if n <= 0:
        return "☆（超时最低档）"
    return "⭐️" * n


def star_text_draw(stars: int) -> str:
    """PIL/CJK 出图用星级（★），避免 emoji 缺字变方框。"""
    n = max(0, min(5, int(stars)))
    if n <= 0:
        return "☆（超时最低档）"
    return "★" * n


def stars_for_draw(text: str) -> str:
    """把文案里的 emoji 星换成 CJK 字体可绘制的 ★。"""
    return str(text).replace("⭐️", "★").replace("⭐", "★")


def format_threshold_lines(
    limits: Dict[int, float], *, adaptive: bool = False, sample_count: int = 0
) -> str:
    mode = "自适应" if adaptive else "默认"
    parts = [f"⭐️×{s}≤{float(limits[s]):.3f}秒" for s in (5, 4, 3, 2, 1)]
    return f"本局阈值（{mode}，样本 {sample_count}）：" + " / ".join(parts)


def distribute_pool(weights: Dict[str, int], pool: int) -> Dict[str, int]:
    """按权重分配整数奖池（最大余数法）。无贡献或奖池≤0 则全 0。"""
    keys = list(weights.keys())
    if not keys:
        return {}
    total_w = sum(max(0, int(weights[k])) for k in keys)
    pool = max(0, int(pool))
    if total_w <= 0 or pool <= 0:
        return {k: 0 for k in keys}
    raw = {k: pool * max(0, int(weights[k])) / total_w for k in keys}
    base = {k: int(v) for k, v in raw.items()}
    rem = pool - sum(base.values())
    order = sorted(keys, key=lambda k: (raw[k] - base[k], weights[k]), reverse=True)
    for k in order:
        if rem <= 0:
            break
        if weights[k] > 0:
            base[k] += 1
            rem -= 1
    return base


@dataclass
class LetterContribution:
    uid: str
    billing_id: int
    name: str
    letter_hits: int = 0
    letter_completes: int = 0
    song_opens: int = 0

    @property
    def weight(self) -> int:
        return (
            self.letter_hits * WEIGHT_LETTER_HIT
            + self.letter_completes * WEIGHT_LETTER_COMPLETE
            + self.song_opens * WEIGHT_SONG_OPEN
        )

    def detail_text(self) -> str:
        parts: List[str] = []
        if self.letter_hits:
            parts.append(f"开字母×{self.letter_hits}")
        if self.letter_completes:
            parts.append(f"补齐×{self.letter_completes}")
        if self.song_opens:
            parts.append(f"开歌×{self.song_opens}")
        return " ".join(parts) if parts else "无贡献"


@dataclass
class LetterPlayerReward:
    uid: str
    billing_id: int
    name: str
    weight: int
    score: int
    break_points: int
    detail: str
    break_capped: bool = False


@dataclass
class LetterSettlement:
    elapsed: float
    stars: int
    score_pool: int
    break_pool: int
    rewards: List[LetterPlayerReward]
    limits: Dict[int, float] = field(default_factory=default_star_limits)
    adaptive: bool = False
    sample_count: int = 0
    reward_multiplier: int = 1

    @property
    def elapsed_text(self) -> str:
        return format_elapsed(self.elapsed)

    @property
    def stars_text(self) -> str:
        return star_text(self.stars)

    @property
    def thresholds_text(self) -> str:
        return format_threshold_lines(
            self.limits, adaptive=self.adaptive, sample_count=self.sample_count
        )

    @property
    def pool_label(self) -> str:
        """奖池一行文案（含限时倍率标注）。"""
        base = f"本局奖池：{self.score_pool} 分 / {self.break_pool} BREAK"
        if self.reward_multiplier > 1:
            return f"{base}（限时×{self.reward_multiplier}；按贡献分配；无贡献不得分）"
        return f"{base}（按贡献分配；无贡献不得分）"


@dataclass
class LetterSong:
    music_id: str
    title: str
    answers: List[str]
    solved: bool = False
    solved_by: str = ""

    def display(self, revealed: Set[str]) -> str:
        chars: List[str] = []
        for ch in self.title:
            if not _is_maskable(ch):
                chars.append(ch)
            elif _norm_token(ch) in revealed or self.solved:
                chars.append(ch)
            else:
                chars.append("?")
        return "".join(chars)

    def hidden_count(self, revealed: Set[str]) -> int:
        if self.solved:
            return 0
        return sum(
            1 for ch in self.title if _is_maskable(ch) and _norm_token(ch) not in revealed
        )

    def is_fully_revealed(self, revealed: Set[str]) -> bool:
        return self.hidden_count(revealed) == 0


@dataclass
class LetterBoard:
    songs: List[LetterSong]
    revealed: Set[str] = field(default_factory=set)
    opened_order: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    starter: str = ""
    contributions: Dict[str, LetterContribution] = field(default_factory=dict)
    # 人多/突发时粘性降级：局中看板可走文字；通关结算仍强制出图
    text_mode: bool = False
    display_mode: str = "auto"  # "auto", "text", "image"
    process_times: List[float] = field(default_factory=list)
    # 非高峰：每人上次有效答题时间；冷却提示只发一次避免刷屏
    last_answer_at: Dict[str, float] = field(default_factory=dict)
    cooldown_notified: Set[str] = field(default_factory=set)

    @property
    def solved_count(self) -> int:
        return sum(1 for s in self.songs if s.solved)

    @property
    def finished(self) -> bool:
        return all(s.solved for s in self.songs)

    @property
    def contributor_count(self) -> int:
        """本局有有效贡献（权重>0）的 uid 数。"""
        return sum(1 for c in self.contributions.values() if c.weight > 0)

    def elapsed(self, now: Optional[float] = None) -> float:
        """已用时；通关定格后固定为 ended_at - started_at，不含后续渲染。"""
        if self.ended_at is not None:
            end = float(self.ended_at)
        else:
            end = time.time() if now is None else float(now)
        return max(0.0, end - float(self.started_at))

    def freeze_end(self, now: Optional[float] = None) -> Optional[float]:
        """通关成功后立刻定格结束时间；未通关或已定格则不动。返回定格后的用时。"""
        if not self.finished:
            return None
        if self.ended_at is None:
            self.ended_at = time.time() if now is None else float(now)
        return self.elapsed()

    def try_consume_answer(
        self, uid: str, *, now: Optional[float] = None
    ) -> Optional[str]:
        """
        开字母专用答题冷却（2.5 秒/人）。

        返回 None：放行（并记录本次时间）。
        返回非空：冷却中，需短提示一次。
        返回空串 ''：冷却中且本轮已提示过，静默丢弃。

        高峰（文字模式）直接放行，不做冷却判断。
        与猜歌 consume_guess_answer_slot（2.5 秒全局）完全独立。
        """
        if self.text_mode or self.prefer_text():
            return None
        key = str(uid or "").strip()
        if not key:
            return None
        t = time.time() if now is None else float(now)
        last = self.last_answer_at.get(key)
        if last is not None:
            remain = LETTER_ANSWER_COOLDOWN_SECONDS - (t - last)
            if remain > 0:
                if key in self.cooldown_notified:
                    return ""
                self.cooldown_notified.add(key)
                return format_guess_answer_rate_limit(remain)
        self.last_answer_at[key] = t
        self.cooldown_notified.discard(key)
        return None

    def note_process(self, now: Optional[float] = None) -> bool:
        """
        记录一次开字母相关处理，并按阈值进入文字模式（本局粘性）。
        条件：贡献人数 ≥ TEXT_MODE_MIN_CONTRIBUTORS
          或 最近 TEXT_MODE_BURST_WINDOW 秒内处理次数 ≥ TEXT_MODE_BURST_COUNT。
        """
        t = time.time() if now is None else float(now)
        cutoff = t - TEXT_MODE_BURST_WINDOW
        self.process_times = [x for x in self.process_times if x >= cutoff]
        self.process_times.append(t)
        if not self.text_mode:
            if self.contributor_count >= TEXT_MODE_MIN_CONTRIBUTORS:
                self.text_mode = True
            elif len(self.process_times) >= TEXT_MODE_BURST_COUNT:
                self.text_mode = True
        return self.text_mode

    def prefer_text(self) -> bool:
        """是否应走文字路径（已粘性进入，或当前贡献人数已达阈值）。"""
        if self.display_mode == "text":
            return True
        if self.display_mode == "image":
            return False
            
        if self.text_mode:
            return True
        if self.contributor_count >= TEXT_MODE_MIN_CONTRIBUTORS:
            self.text_mode = True
            return True
        return False

    def ensure_contribution(
        self, uid: str, billing_id: int, name: str
    ) -> LetterContribution:
        key = str(uid)
        c = self.contributions.get(key)
        if c is None:
            c = LetterContribution(
                uid=key, billing_id=int(billing_id), name=name or key
            )
            self.contributions[key] = c
        else:
            c.billing_id = int(billing_id)
            if name:
                c.name = name
        return c

    def claim_fully_revealed(self, solver: str) -> List[LetterSong]:
        """标题字母已齐：记在补齐字母的人身上并立刻解开。"""
        newly: List[LetterSong] = []
        for song in self.songs:
            if song.solved or not song.is_fully_revealed(self.revealed):
                continue
            song.solved = True
            song.solved_by = solver or "字母补齐"
            newly.append(song)
        if newly:
            self.freeze_end()
        return newly

    def settle(
        self,
        *,
        now: Optional[float] = None,
        limits: Optional[Dict[int, float]] = None,
        adaptive: bool = False,
        sample_count: int = 0,
        event_now: Optional[float] = None,
    ) -> LetterSettlement:
        """全部解开后的通关结算（不玩了勿调用）。

        event_now：活动倍率判定用墙钟（默认 time.time()）；测试可传入固定值。
        用时以 freeze_end 定格值为准，不含后续渲染/发奖耗时。
        """
        self.freeze_end(now)
        elapsed = self.elapsed()
        caps = limits or default_star_limits()
        stars = star_for_elapsed(elapsed, caps)
        mult = letter_reward_multiplier(now=event_now)
        score_pool = SCORE_POOL_BY_STAR.get(stars, SCORE_POOL_BY_STAR[0]) * mult
        weights = {
            uid: c.weight for uid, c in self.contributions.items() if c.weight > 0
        }
        score_map = distribute_pool(weights, score_pool)

        # BREAK 按「完整开出曲目数」计数，不随星级/贡献权重分配：
        # 每完整开出一首(song_opens 直接猜中 + letter_completes 字母补齐)
        # 的人得 1；开出数量最多者(含并列)得 2。
        # 0 星(超时极慢通关)不发 BREAK；限时倍率整体放大。
        break_map: Dict[str, int] = {}
        openers = {
            uid: c.song_opens + c.letter_completes
            for uid, c in self.contributions.items()
            if c.song_opens + c.letter_completes > 0
        }
        if stars >= 1 and openers:
            top_opens = max(openers.values())
            for uid, n in openers.items():
                break_map[uid] = (2 if n == top_opens else 1) * mult
        break_pool = sum(break_map.values())

        rewards: List[LetterPlayerReward] = []
        for uid, c in sorted(
            self.contributions.items(),
            key=lambda item: (-item[1].weight, item[1].name),
        ):
            if c.weight <= 0:
                continue
            rewards.append(
                LetterPlayerReward(
                    uid=uid,
                    billing_id=c.billing_id,
                    name=c.name,
                    weight=c.weight,
                    score=score_map.get(uid, 0),
                    break_points=break_map.get(uid, 0),
                    detail=c.detail_text(),
                )
            )
        return LetterSettlement(
            elapsed=elapsed,
            stars=stars,
            score_pool=score_pool,
            break_pool=break_pool,
            rewards=rewards,
            limits=dict(caps),
            adaptive=adaptive,
            sample_count=sample_count,
            reward_multiplier=mult,
        )


def format_settlement_message(settlement: LetterSettlement) -> str:
    """通关结算短文案（详情放分成图）。"""
    lines = [
        f"🎉 全部解开！用时 {settlement.elapsed_text} · {settlement.stars_text}",
        settlement.pool_label,
    ]
    if not settlement.rewards:
        lines.append("本局无人有效贡献，不发奖。")
    return "\n".join(lines)


def format_board_text(board: LetterBoard) -> str:
    """纯文字看板（人多/突发时用，避免 PIL 出图）。"""
    lines = [
        (
            f"【舞萌开字母】进度 {board.solved_count}/{len(board.songs)}"
            f" · 已开 {len(board.revealed)} 字"
            f" · 用时 {format_elapsed(board.elapsed())}"
        )
    ]
    for idx, song in enumerate(board.songs, 1):
        # 文字看板用 emoji；PIL 出图仍用 [OK]/[??] 避免缺字
        icon = "✅" if song.solved else "🤔"
        lines.append(f"{idx}. {icon} {song.display(board.revealed)}")
    opened = ", ".join(board.opened_order) if board.opened_order else "（还没有）"
    lines.append(f"已开：{opened}")
    return "\n".join(lines)


def format_settlement_ranking_text(settlement: LetterSettlement) -> str:
    """文字版贡献分成榜（人多结算用）。"""
    lines = [
        f"🎉 全部解开！用时 {settlement.elapsed_text} · {settlement.stars_text}",
        settlement.pool_label,
    ]
    if not settlement.rewards:
        lines.append("本局无人有效贡献，不发奖。")
        return "\n".join(lines)
    capped = False
    for i, r in enumerate(settlement.rewards, 1):
        detail = f"（{r.detail}）" if r.detail and r.detail != "无贡献" else ""
        bp_text = f" +{r.break_points}BREAK"
        if r.break_capped:
            capped = True
            bp_text += " ⚠️"
        lines.append(
            f"#{i} {r.name}  权重{r.weight} → +{r.score}分{bp_text}{detail}"
        )
    if capped:
        lines.append("")
        lines.append("⚠️ 用户该游戏达今日上限，不再发放奖励")
    return "\n".join(lines)


def format_settlement_text(settlement: LetterSettlement, board: LetterBoard) -> str:
    """通关结算纯文字：分成榜 + 终局看板（一条消息）。"""
    return "\n".join(
        [format_settlement_ranking_text(settlement), format_board_text(board)]
    )


def _format_letter_complete(songs: List[LetterSong]) -> str:
    if not songs:
        return ""
    names = "、".join(s.title for s in songs)
    return f"\n✅ 字母补齐：{names}"


class LetterGuessManager:
    """群开字母会话（与猜歌互斥）。"""

    Group: Dict[GroupId, LetterBoard] = {}

    def is_playing(self, gid: GroupId) -> bool:
        return gid in self.Group

    def is_busy_with_guess(self, gid: GroupId) -> bool:
        return guess.is_busy(gid) or self.is_playing(gid)

    def end(self, gid: GroupId) -> Optional[LetterBoard]:
        from .maimaidx_game_session import game_session_gate
        game_session_gate.release(gid)
        return self.Group.pop(gid, None)

    def get(self, gid: GroupId) -> Optional[LetterBoard]:
        return self.Group.get(gid)

    def _answers_for(self, music: Music) -> List[str]:
        answers: List[str] = []
        try:
            alias_rows = mai.total_alias_list.by_id(music.id)
            if alias_rows:
                answers.extend(str(a) for a in alias_rows[0].Alias)
        except Exception:
            pass
        answers.append(music.title)
        answers.append(str(music.id))
        # 去重保序
        seen: Set[str] = set()
        ordered: List[str] = []
        for a in answers:
            key = a.lower()
            if key in seen or not a:
                continue
            seen.add(key)
            ordered.append(a)
        return ordered

    def _pick_songs(self, count: int = BOARD_SIZE) -> List[Music]:
        pool = guess._guess_music_pool()
        # 优先拉丁字母较多的标题，玩法更接近截图
        latin_rich = [m for m in pool if _latin_letter_count(m.title) >= 4]
        maskable = [m for m in pool if _title_maskable_count(m.title) >= 3]
        primary = latin_rich or maskable or pool
        random.shuffle(primary)
        picked: List[Music] = []
        used_titles: Set[str] = set()
        for music in primary:
            title_key = music.title.strip().lower()
            if title_key in used_titles:
                continue
            if _title_maskable_count(music.title) < 2:
                continue
            picked.append(music)
            used_titles.add(title_key)
            if len(picked) >= count:
                break
        if len(picked) < count:
            rest = [m for m in pool if m not in picked]
            random.shuffle(rest)
            for music in rest:
                picked.append(music)
                if len(picked) >= count:
                    break
        return picked[:count]

    def start(
        self,
        gid: GroupId,
        *,
        starter: str = "",
        count: int = BOARD_SIZE,
        display_mode: str = "auto",
    ) -> LetterBoard:
        musics = self._pick_songs(count)
        if len(musics) < 3:
            raise RuntimeError("可用曲目不足，暂时无法开局")
        songs = [
            LetterSong(
                music_id=str(m.id),
                title=m.title,
                answers=self._answers_for(m),
            )
            for m in musics
        ]
        board = LetterBoard(songs=songs, starter=starter, started_at=time.time(),
                            display_mode=display_mode)
        # 开局不预开任何字母
        self.Group[gid] = board
        titles = " / ".join(s.title for s in songs)
        log.info(f"[LetterGuess] 开局 gid={gid} songs={titles}")
        return board

    def open_letter(
        self,
        gid: GroupId,
        raw: str,
        *,
        solver: str = "",
        uid: str = "",
        billing_id: int = 0,
    ) -> Tuple[str, LetterBoard, List[LetterSong], dict[str, int]]:
        """开字母。返回 (文案, 看板, 本回合补齐的曲, 补齐前各曲隐藏字符数)。"""
        board = self.Group[gid]
        token = raw.strip()
        if not token:
            return "请发送要开的字母，例如：开字母 m", board, [], {}
        # 只取第一个字符；兼容 "开字母 m" 已拆好的单字
        ch = token[0]
        if not _is_maskable(ch):
            return "只能开字母、数字或日文/汉字字符哦", board, [], {}
        key = _norm_token(ch)
        hidden_before = {
            song.music_id: song.hidden_count(board.revealed)
            for song in board.songs
            if not song.solved
        }
        if key in board.revealed:
            # 历史对局可能已全开字母却未 claim，再跑一次避免卡在未解开
            completed = board.claim_fully_revealed(solver)
            if completed and uid:
                c = board.ensure_contribution(uid, billing_id, solver)
                c.letter_completes += len(completed)
            if completed:
                # 仅补齐仍提示；「已经开过了」本身静默，防刷屏
                msg = _format_letter_complete(completed).lstrip("\n")
                return msg, board, completed, hidden_before
            # 空文案 = 已开过且无新进展，调用方应静默忽略
            return "", board, [], hidden_before

        board.revealed.add(key)
        board.opened_order.append(key)
        hit = 0
        for song in board.songs:
            if song.solved:
                continue
            hit += sum(1 for c in song.title if _norm_token(c) == key)
        completed = board.claim_fully_revealed(solver)
        if uid and (hit > 0 or completed):
            c = board.ensure_contribution(uid, billing_id, solver)
            if hit > 0:
                c.letter_hits += 1
            if completed:
                c.letter_completes += len(completed)
        if hit <= 0:
            msg = f"没有「{key}」呢…"
        else:
            msg = f"开出「{key}」，揭示 {hit} 处"
        msg += _format_letter_complete(completed)
        return msg, board, completed, hidden_before

    def open_song(
        self,
        gid: GroupId,
        guess_text: str,
        *,
        solver: str = "",
        uid: str = "",
        billing_id: int = 0,
    ) -> Tuple[str, LetterBoard, Optional[LetterSong], List[LetterSong], dict[str, int]]:
        board = self.Group[gid]
        text = guess_text.strip()
        if not text:
            return "请发送歌名或别名，例如：开歌 conflict", board, None, [], {}
        hidden_before = {
            song.music_id: song.hidden_count(board.revealed)
            for song in board.songs
            if not song.solved
        }
        for song in board.songs:
            if song.solved:
                continue
            # 开字母是抢答玩法，短字符串的一字符容错会让 ``the`` 误中
            # ``she`` / ``tie`` 等实际并不包含输入内容的曲名或别名。
            # 保留大小写、标点归一化和真实子串匹配，但禁止错字近似命中。
            if match_guess_answer(
                text,
                song.answers,
                allow_latin_typo=False,
            ):
                song.solved = True
                song.solved_by = solver or "开歌"
                # 猜中时把该曲剩余字母也记入已开集合，便于展示
                for ch in song.title:
                    if _is_maskable(ch):
                        key = _norm_token(ch)
                        if key not in board.revealed:
                            board.revealed.add(key)
                            board.opened_order.append(key)
                # 附带补齐的其它曲归当前开歌者
                completed = board.claim_fully_revealed(solver)
                board.freeze_end()
                if uid:
                    c = board.ensure_contribution(uid, billing_id, solver)
                    c.song_opens += 1
                    if completed:
                        c.letter_completes += len(completed)
                msg = f"✅ 猜中：{song.title}"
                msg += _format_letter_complete(completed)
                return msg, board, song, completed, hidden_before
        return "没有对上未解开的歌，再想想？", board, None, [], hidden_before

    def reveal_all(self, board: LetterBoard) -> None:
        for song in board.songs:
            song.solved = True
            if not song.solved_by:
                song.solved_by = "结束"


letter_guess = LetterGuessManager()


def _board_font() -> Path:
    for candidate in (
        SIYUAN,
        TBFONT,
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ):
        try:
            path = Path(candidate)
            if path.exists():
                return path
        except Exception:
            continue
    return Path(SIYUAN)


def render_letter_board(board: LetterBoard) -> Image.Image:
    """绘制开字母看板图。"""
    width = 900
    row_h = 52
    header_h = 110
    footer_h = 150
    height = header_h + row_h * len(board.songs) + footer_h
    im = Image.new("RGBA", (width, height), _BG)
    dr = ImageDraw.Draw(im)
    # 曲名可能含中日文/扩展拉丁，必须用 CJK 字体；Torus 等西文字体会豆腐字。
    font_path = _board_font()
    title_font = DrawText(dr, font_path)

    elapsed_text = format_elapsed(board.elapsed())
    dr.rounded_rectangle((24, 24, width - 24, height - 24), radius=22, fill=_CARD)
    title_font.draw(48, 44, 40, "舞萌开字母", _TITLE, "lt", 2, (0, 0, 0, 120))
    title_font.draw(
        48,
        92,
        18,
        (
            f"进度 {board.solved_count}/{len(board.songs)}"
            f"  ·  已开 {len(board.revealed)} 个字符"
            f"  ·  已用时 {elapsed_text}"
        ),
        _MUTED,
        "lt",
    )

    y = header_h
    for idx, song in enumerate(board.songs, 1):
        if idx > 1:
            dr.line((48, y, width - 48, y), fill=_LINE, width=1)
        if song.solved:
            icon, color = "[OK]", _OK
        else:
            icon, color = "[??]", _WAIT
        shown = song.display(board.revealed)
        title_font.draw(56, y + 14, 22, f"{idx}.", _MUTED, "lt")
        title_font.draw(96, y + 14, 22, icon, color, "lt")
        title_font.draw(170, y + 14, 24, shown, _TEXT, "lt")
        y += row_h

    opened = ", ".join(board.opened_order) if board.opened_order else "（还没有）"
    title_font.draw(48, y + 18, 22, "已开出字母:", _TITLE, "lt")
    # 字母列表可能很长，自动换行
    line = ""
    lines: List[str] = []
    for part in opened.split(", "):
        trial = part if not line else f"{line}, {part}"
        # 粗略按字符数折行
        if len(trial) > 48:
            if line:
                lines.append(line)
            line = part
        else:
            line = trial
    if line:
        lines.append(line)
    ty = y + 52
    for row in lines[:4]:
        title_font.draw(48, ty, 18, row, _MUTED, "lt")
        ty += 26

    title_font.draw(
        width // 2,
        height - 28,
        14,
        letter_image_footer(),
        _MUTED,
        "mm",
    )
    return im


def letter_image_footer() -> str:
    """开字母相关图片统一底部文案。"""
    return f"舞萌开字母 | {footer_generated()}"


def board_image_segment(board: LetterBoard):
    from nonebot.adapters.onebot.v11 import MessageSegment

    return MessageSegment.image(image_to_base64(render_letter_board(board)))
