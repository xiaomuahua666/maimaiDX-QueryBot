"""猜铺面：用 Chart Preview 无音乐录制页生成谱面视频并缓存。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import functools
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import wave
from array import array
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import httpx
from loguru import logger as log
from playwright.async_api import async_playwright

_PKG_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHART_CDN = 'https://assets2.lxns.net/maimai/chart'
# rev=8：多核并行录制/转码（同曲两阶段并行 + 预制并发）
CHART_VIDEO_REV = 8
DEFAULT_DURATION = 40
# 整局 120 秒：前 90 秒静音，最后 30 秒曲末带 BGM
PHASE2_DURATION = 30
STAGE_INTERVAL = 90
STAGE_FINAL_GRACE = 30
DEFAULT_VIEWPORT = 720
ANSWER_GRACE = STAGE_INTERVAL + STAGE_FINAL_GRACE  # 120
# 作答倒计时提醒节点（秒）
COUNTDOWN_MARKS = (60, 30, 20, 10)
MAX_HIT_SOUNDS = 2500
DEFAULT_CHART_BATCH_LIMIT = 20
# 30fps：高负载下 60fps 录制易掉帧；编码侧统一 CFR，观感更稳
CAPTURE_FPS = 30
VIDEO_CRF = 18
# medium：预制流畅优先；可用环境变量改回 faster 提速
VIDEO_PRESET = os.environ.get('MAIMAIDX_CHART_VIDEO_PRESET', 'medium').strip() or 'medium'
AUDIO_BITRATE = '192k'
BASE64_CHUNK_CHARS = 512 * 1024
FFMPEG_NICE = 15
BG_FILL_IDLE_SLEEP_SEC = 180
BG_FILL_BUSY_SLEEP_SEC = 8
BG_FILL_STARTUP_DELAY_SEC = 45


def _cpu_count() -> int:
    return max(1, int(os.cpu_count() or 4))


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取正整数环境变量；minimum=0 时允许关闭（如 BG_FILL_WORKERS=0）。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


def _default_render_workers() -> int:
    """在线录制槽：宁少勿多，避免 Chromium 饿死查分/传分。"""
    n = _cpu_count()
    # 多核机默认 2；硬顶 3。低峰可用 MAIMAIDX_CHART_RENDER_WORKERS 加大。
    if n >= 16:
        return 2
    return 1


def _default_batch_song_workers() -> int:
    """预制时并发曲目数；每曲最多占 2 个录制槽（静音+BGM）。"""
    return max(1, min(2, _default_render_workers()))


def _default_ffmpeg_threads() -> int:
    """单路 ffmpeg 线程；总占用约 render×threads，勿打满整机。"""
    return 2


def _default_cpu_pool_workers() -> int:
    """ffmpeg 线程池：与 bot 错峰，默认保守。"""
    return max(2, min(4, _cpu_count() // 8 or 2))


def _default_bg_fill_workers() -> int:
    """启动后后台补 BGM；默认 1。设 0 可完全关闭。"""
    return 1


def _default_render_max() -> int:
    return 3 if _cpu_count() >= 16 else 2


def _default_bg_fill_max() -> int:
    return 1


# 环境变量：
# MAIMAIDX_CHART_ADAPTIVE / RENDER_*_MIN|MAX / BG_FILL_*_MIN|MAX
# MAIMAIDX_CHART_RENDER_WORKERS / BATCH_SONGS / FFMPEG_THREADS
# MAIMAIDX_CHART_CPU_POOL / BG_FILL_WORKERS / VIDEO_PRESET
ADAPTIVE_ENABLED = _env_bool('MAIMAIDX_CHART_ADAPTIVE', True)
ADAPTIVE_INTERVAL_SEC = max(5, _env_int('MAIMAIDX_CHART_ADAPTIVE_INTERVAL', 20))
ADAPTIVE_WARMUP_SEC = max(0, _env_int('MAIMAIDX_CHART_ADAPTIVE_WARMUP', 60, minimum=0))
# load1 / ncpu：idle < elevated < busy < critical
ADAPTIVE_LOAD_IDLE = _env_float('MAIMAIDX_CHART_LOAD_IDLE', 0.25)
ADAPTIVE_LOAD_ELEVATED = _env_float('MAIMAIDX_CHART_LOAD_ELEVATED', 0.35)
ADAPTIVE_LOAD_BUSY = _env_float('MAIMAIDX_CHART_LOAD_BUSY', 0.50)
ADAPTIVE_LOAD_CRIT = _env_float('MAIMAIDX_CHART_LOAD_CRIT', 0.70)
# 事件循环延迟（ms）：sleep(50ms) 的 overrun
ADAPTIVE_LAG_BUSY_MS = _env_float('MAIMAIDX_CHART_LAG_BUSY_MS', 80.0)
ADAPTIVE_LAG_CRIT_MS = _env_float('MAIMAIDX_CHART_LAG_CRIT_MS', 200.0)

RENDER_MIN = _env_int('MAIMAIDX_CHART_RENDER_MIN', 1)
RENDER_MAX = _env_int('MAIMAIDX_CHART_RENDER_MAX', _default_render_max())
BG_FILL_MIN = _env_int('MAIMAIDX_CHART_BG_FILL_MIN', 0, minimum=0)
BG_FILL_MAX = _env_int('MAIMAIDX_CHART_BG_FILL_MAX', _default_bg_fill_max(), minimum=0)
BATCH_SONG_MIN = _env_int('MAIMAIDX_CHART_BATCH_SONGS_MIN', 1)
BATCH_SONG_MAX = _env_int(
    'MAIMAIDX_CHART_BATCH_SONGS_MAX',
    max(1, min(2, RENDER_MAX)),
)
# 兼容：只写了旧变量、未写 MAX 时，把旧变量当作上限
if os.environ.get('MAIMAIDX_CHART_RENDER_WORKERS') and not os.environ.get(
    'MAIMAIDX_CHART_RENDER_MAX'
):
    RENDER_MAX = _env_int('MAIMAIDX_CHART_RENDER_WORKERS', RENDER_MAX)
if os.environ.get('MAIMAIDX_CHART_BG_FILL_WORKERS') and not os.environ.get(
    'MAIMAIDX_CHART_BG_FILL_MAX'
):
    BG_FILL_MAX = _env_int(
        'MAIMAIDX_CHART_BG_FILL_WORKERS', BG_FILL_MAX, minimum=0,
    )
if os.environ.get('MAIMAIDX_CHART_BATCH_SONGS') and not os.environ.get(
    'MAIMAIDX_CHART_BATCH_SONGS_MAX'
):
    BATCH_SONG_MAX = _env_int('MAIMAIDX_CHART_BATCH_SONGS', BATCH_SONG_MAX)

RENDER_MIN = max(1, min(RENDER_MIN, RENDER_MAX))
RENDER_MAX = max(RENDER_MIN, RENDER_MAX)
BG_FILL_MIN = max(0, min(BG_FILL_MIN, BG_FILL_MAX))
BG_FILL_MAX = max(BG_FILL_MIN, BG_FILL_MAX)
BATCH_SONG_MIN = max(1, min(BATCH_SONG_MIN, BATCH_SONG_MAX))
BATCH_SONG_MAX = max(BATCH_SONG_MIN, BATCH_SONG_MAX)

# 运行时可变；自适应开启时从保守档起步，勿一启动打满
if ADAPTIVE_ENABLED:
    RENDER_WORKERS = RENDER_MIN
    BG_FILL_WORKERS = BG_FILL_MIN
    BATCH_SONG_WORKERS = BATCH_SONG_MIN
else:
    RENDER_WORKERS = _env_int(
        'MAIMAIDX_CHART_RENDER_WORKERS', _default_render_workers(),
    )
    BATCH_SONG_WORKERS = _env_int(
        'MAIMAIDX_CHART_BATCH_SONGS', _default_batch_song_workers(),
    )
    BG_FILL_WORKERS = _env_int(
        'MAIMAIDX_CHART_BG_FILL_WORKERS', _default_bg_fill_workers(), minimum=0,
    )

FFMPEG_THREADS = _env_int('MAIMAIDX_CHART_FFMPEG_THREADS', _default_ffmpeg_threads())
CPU_POOL_WORKERS = _env_int('MAIMAIDX_CHART_CPU_POOL', _default_cpu_pool_workers())
# 负载超过该阈值时跳过本轮后台补洞（load1 / nproc）；与 elevated 档对齐
BG_FILL_LOAD_RATIO = _env_float(
    'MAIMAIDX_CHART_BG_FILL_LOAD_RATIO', ADAPTIVE_LOAD_ELEVATED,
)
CHROMIUM_NICE = _env_int('MAIMAIDX_CHART_CHROMIUM_NICE', FFMPEG_NICE, minimum=0)
CHART_DIFF_NAMES = {
    2: '绿',
    3: '黄',
    4: '红',
    5: '紫',
    6: '白',
}

CHART_GUESS_DIR = _PKG_ROOT / 'data' / 'chart_guess'
CHART_GUESS_CACHE_DIR = CHART_GUESS_DIR / 'cache'
CHART_GUESS_MANIFEST = CHART_GUESS_DIR / 'manifest.json'

_BUILD_LOCKS: Dict[str, asyncio.Lock] = {}
_static_server: Optional[ThreadingHTTPServer] = None
_static_port: Optional[int] = None
_static_lock = threading.Lock()
_prepare_status = ''
_prepare_status_lock = threading.Lock()
_batch_cancel = threading.Event()
_render_sem: Optional['_AdjustableSemaphore'] = None
_bg_fill_sem: Optional['_AdjustableSemaphore'] = None
_batch_song_sem: Optional['_AdjustableSemaphore'] = None
_cpu_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_bg_fill_task: Optional[asyncio.Task] = None
_adaptive_task: Optional[asyncio.Task] = None
_adaptive_started_at = 0.0
_adaptive_tier = 'init'
_adaptive_up_streak = 0
_CACHE_KEY_RE = re.compile(
    r'^(?P<mid>.+)_(?P<kind>standard|dx|utage)_(?P<diff>\d+)_r(?P<rev>\d+)$'
)


class _AdjustableSemaphore:
    """可动态调整容量的信号量；容量为 0 时 acquire 会等待。"""

    def __init__(self, value: int) -> None:
        self._cap = max(0, int(value))
        self._used = 0
        self._cond: Optional[asyncio.Condition] = None

    def _ensure_cond(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def in_use(self) -> int:
        return self._used

    async def acquire(self) -> None:
        cond = self._ensure_cond()
        async with cond:
            while self._used >= self._cap:
                await cond.wait()
            self._used += 1

    async def release(self) -> None:
        cond = self._ensure_cond()
        async with cond:
            self._used = max(0, self._used - 1)
            cond.notify_all()

    async def set_capacity(self, value: int) -> None:
        cond = self._ensure_cond()
        async with cond:
            self._cap = max(0, int(value))
            cond.notify_all()

    async def __aenter__(self) -> '_AdjustableSemaphore':
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


def _get_render_sem() -> _AdjustableSemaphore:
    global _render_sem
    if _render_sem is None:
        _render_sem = _AdjustableSemaphore(RENDER_WORKERS)
        log.info(
            f'[GuessChart] 并行度 cpu={_cpu_count()} adaptive={int(ADAPTIVE_ENABLED)} '
            f'render={RENDER_WORKERS}[{RENDER_MIN}-{RENDER_MAX}] '
            f'batch_songs={BATCH_SONG_WORKERS}[{BATCH_SONG_MIN}-{BATCH_SONG_MAX}] '
            f'bg_fill={BG_FILL_WORKERS}[{BG_FILL_MIN}-{BG_FILL_MAX}] '
            f'cpu_pool={CPU_POOL_WORKERS} ffmpeg_threads={FFMPEG_THREADS} '
            f'preset={VIDEO_PRESET} fps={CAPTURE_FPS}'
        )
    return _render_sem


def _get_bg_fill_sem() -> _AdjustableSemaphore:
    global _bg_fill_sem
    if _bg_fill_sem is None:
        _bg_fill_sem = _AdjustableSemaphore(max(0, BG_FILL_WORKERS))
    return _bg_fill_sem


def _get_batch_song_sem() -> _AdjustableSemaphore:
    global _batch_song_sem
    if _batch_song_sem is None:
        _batch_song_sem = _AdjustableSemaphore(BATCH_SONG_WORKERS)
    return _batch_song_sem


def _format_duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, value = divmod(value, 3600)
    minutes, secs = divmod(value, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


def _get_cpu_executor() -> concurrent.futures.ThreadPoolExecutor:
    """ffmpeg 专用线程池：不占用默认线程池，避免饿死查分等 to_thread。"""
    global _cpu_executor
    if _cpu_executor is None:
        _cpu_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=CPU_POOL_WORKERS,
            thread_name_prefix='chart-ffmpeg',
        )
        log.info(f'[GuessChart] ffmpeg 线程池 workers={CPU_POOL_WORKERS}')
    return _cpu_executor


async def _to_cpu(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_cpu_executor(),
        functools.partial(func, *args, **kwargs),
    )


def _low_priority_cmd(cmd: Sequence[str]) -> List[str]:
    """用 nice 降低 ffmpeg 优先级，避免饿死查分/传分。"""
    if os.name == 'nt':
        return list(cmd)
    return ['nice', '-n', str(FFMPEG_NICE), *cmd]


def _system_load_ratio() -> float:
    try:
        return float(os.getloadavg()[0]) / float(_cpu_count())
    except (AttributeError, OSError, ZeroDivisionError):
        return 0.0


def _bg_fill_should_pause() -> bool:
    """在线高峰：load 偏高时跳过后台补洞，把核留给查分/消息。"""
    if BG_FILL_WORKERS <= 0:
        return True
    return _system_load_ratio() >= max(0.05, BG_FILL_LOAD_RATIO)


def _adaptive_targets(
    load: float,
    lag_ms: float,
    *,
    warmup: bool = False,
) -> Tuple[int, int, int, str]:
    """
    根据负载与事件循环延迟给出目标并发。
    保主功能：优先砍 BG_FILL，再砍 RENDER；忙时硬底 RENDER_MIN / BG_FILL=0。
    """
    if warmup:
        return RENDER_MIN, BG_FILL_MIN, BATCH_SONG_MIN, 'warmup'

    if load >= ADAPTIVE_LOAD_CRIT or lag_ms >= ADAPTIVE_LAG_CRIT_MS:
        return RENDER_MIN, 0, BATCH_SONG_MIN, 'critical'
    if load >= ADAPTIVE_LOAD_BUSY or lag_ms >= ADAPTIVE_LAG_BUSY_MS:
        render = max(RENDER_MIN, min(RENDER_MAX, 2))
        return render, 0, BATCH_SONG_MIN, 'busy'
    if load >= ADAPTIVE_LOAD_ELEVATED:
        render = max(RENDER_MIN, min(RENDER_MAX, 2))
        return render, 0, min(BATCH_SONG_MAX, max(BATCH_SONG_MIN, 2)), 'elevated'
    if load >= ADAPTIVE_LOAD_IDLE:
        render = max(RENDER_MIN, min(RENDER_MAX, 3))
        bg = min(BG_FILL_MAX, max(BG_FILL_MIN, 1)) if BG_FILL_MAX > 0 else 0
        batch = min(BATCH_SONG_MAX, max(BATCH_SONG_MIN, min(2, render)))
        return render, bg, batch, 'normal'
    # idle：允许升到配置上限
    return RENDER_MAX, BG_FILL_MAX, BATCH_SONG_MAX, 'idle'


def _step_toward(current: int, target: int, *, aggressive_down: bool) -> int:
    """平滑调整：升档每次 +1；降档可一次到位（保主功能）。"""
    if target == current:
        return current
    if target < current:
        return target if aggressive_down else current - 1
    return current + 1


async def _measure_loop_lag_ms(probe_sec: float = 0.05) -> float:
    """用 sleep overrun 估计事件循环拥塞（近似消息处理延迟）。"""
    t0 = time.perf_counter()
    await asyncio.sleep(probe_sec)
    overrun = time.perf_counter() - t0 - probe_sec
    return max(0.0, overrun * 1000.0)


async def _apply_adaptive_workers(
    render: int,
    bg_fill: int,
    batch: int,
    *,
    tier: str,
    load: float,
    lag_ms: float,
) -> None:
    global RENDER_WORKERS, BG_FILL_WORKERS, BATCH_SONG_WORKERS, _adaptive_tier
    render = max(RENDER_MIN, min(RENDER_MAX, int(render)))
    bg_fill = max(BG_FILL_MIN, min(BG_FILL_MAX, int(bg_fill)))
    batch = max(BATCH_SONG_MIN, min(BATCH_SONG_MAX, int(batch)))
    # 保主：忙/升温档与高 load 时强制关掉补洞
    if tier == 'warmup':
        bg_fill = min(bg_fill, BG_FILL_MIN)
    elif tier in ('critical', 'busy', 'elevated') or load >= BG_FILL_LOAD_RATIO:
        bg_fill = 0

    changed = (
        render != RENDER_WORKERS
        or bg_fill != BG_FILL_WORKERS
        or batch != BATCH_SONG_WORKERS
        or tier != _adaptive_tier
    )
    RENDER_WORKERS = render
    BG_FILL_WORKERS = bg_fill
    BATCH_SONG_WORKERS = batch
    _adaptive_tier = tier

    rsem = _get_render_sem()
    bsem = _get_bg_fill_sem()
    ssem = _get_batch_song_sem()
    if rsem.capacity != render:
        await rsem.set_capacity(render)
    if bsem.capacity != bg_fill:
        await bsem.set_capacity(bg_fill)
    if ssem.capacity != batch:
        await ssem.set_capacity(batch)

    msg = (
        f'[GuessChart] 自适应档位={tier} load={load:.2f} lag={lag_ms:.0f}ms '
        f'render={render}/{RENDER_MAX} bg_fill={bg_fill}/{BG_FILL_MAX} '
        f'batch={batch}/{BATCH_SONG_MAX}'
    )
    if changed:
        log.info(msg)
    else:
        log.debug(msg)


async def _chart_adaptive_loop() -> None:
    global _adaptive_started_at, _adaptive_up_streak
    _adaptive_started_at = time.time()
    log.info(
        f'[GuessChart] 自适应并发已启动 interval={ADAPTIVE_INTERVAL_SEC}s '
        f'warmup={ADAPTIVE_WARMUP_SEC}s '
        f'render[{RENDER_MIN}-{RENDER_MAX}] bg_fill[{BG_FILL_MIN}-{BG_FILL_MAX}] '
        f'load_idle<{ADAPTIVE_LOAD_IDLE:.2f} elevated>={ADAPTIVE_LOAD_ELEVATED:.2f} '
        f'busy>={ADAPTIVE_LOAD_BUSY:.2f} crit>={ADAPTIVE_LOAD_CRIT:.2f} '
        f'lag_busy>={ADAPTIVE_LAG_BUSY_MS:.0f}ms lag_crit>={ADAPTIVE_LAG_CRIT_MS:.0f}ms'
    )
    # 立即落到保守档，避免启动瞬间打满
    await _apply_adaptive_workers(
        RENDER_MIN, BG_FILL_MIN, BATCH_SONG_MIN,
        tier='warmup', load=_system_load_ratio(), lag_ms=0.0,
    )
    while True:
        try:
            await asyncio.sleep(ADAPTIVE_INTERVAL_SEC)
            load = _system_load_ratio()
            lag_ms = await _measure_loop_lag_ms()
            warmup = (
                ADAPTIVE_WARMUP_SEC > 0
                and (time.time() - _adaptive_started_at) < ADAPTIVE_WARMUP_SEC
            )
            tgt_r, tgt_b, tgt_batch, tier = _adaptive_targets(
                load, lag_ms, warmup=warmup,
            )
            # 升档需连续 2 次同向，抑抖；降档立即响应
            going_up = (
                tgt_r > RENDER_WORKERS
                or tgt_b > BG_FILL_WORKERS
                or tgt_batch > BATCH_SONG_WORKERS
            )
            going_down = (
                tgt_r < RENDER_WORKERS
                or tgt_b < BG_FILL_WORKERS
                or tgt_batch < BATCH_SONG_WORKERS
            )
            if going_down:
                _adaptive_up_streak = 0
                next_r = _step_toward(
                    RENDER_WORKERS, tgt_r, aggressive_down=True,
                )
                next_b = _step_toward(
                    BG_FILL_WORKERS, tgt_b, aggressive_down=True,
                )
                next_batch = _step_toward(
                    BATCH_SONG_WORKERS, tgt_batch, aggressive_down=True,
                )
            elif going_up:
                _adaptive_up_streak += 1
                if _adaptive_up_streak < 2 and not warmup:
                    next_r, next_b, next_batch = (
                        RENDER_WORKERS, BG_FILL_WORKERS, BATCH_SONG_WORKERS,
                    )
                    tier = _adaptive_tier or tier
                else:
                    next_r = _step_toward(
                        RENDER_WORKERS, tgt_r, aggressive_down=False,
                    )
                    # 升 RENDER 之前先确认 load 允许再开 BG_FILL
                    next_b = _step_toward(
                        BG_FILL_WORKERS, tgt_b, aggressive_down=False,
                    )
                    next_batch = _step_toward(
                        BATCH_SONG_WORKERS, tgt_batch, aggressive_down=False,
                    )
            else:
                _adaptive_up_streak = 0
                next_r, next_b, next_batch = tgt_r, tgt_b, tgt_batch

            await _apply_adaptive_workers(
                next_r, next_b, next_batch,
                tier=tier, load=load, lag_ms=lag_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f'[GuessChart] 自适应循环异常: {e}')
            await asyncio.sleep(ADAPTIVE_INTERVAL_SEC)


def _renice_pid(pid: int, nice: int) -> None:
    if os.name == 'nt' or pid <= 0:
        return
    try:
        subprocess.run(
            ['renice', '-n', str(nice), '-p', str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        pass


def _renice_playwright_chromium(nice: int = CHROMIUM_NICE) -> int:
    """降低本机 ms-playwright chromium 优先级（best-effort）。"""
    if os.name == 'nt' or nice <= 0:
        return 0
    try:
        out = subprocess.run(
            ['pgrep', '-f', 'ms-playwright/chromium'],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return 0
    n = 0
    for line in (out.stdout or '').splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        _renice_pid(int(line), nice)
        n += 1
    return n


def _run_cmd(cmd: Sequence[str], *, low_priority: bool = True) -> subprocess.CompletedProcess:
    final = _low_priority_cmd(cmd) if low_priority else list(cmd)
    return subprocess.run(final, capture_output=True, text=True)


def _ffmpeg_x264_args() -> List[str]:
    # 固定 GOP + CFR，减轻掉帧后的抖动/花屏观感
    return [
        '-threads', str(FFMPEG_THREADS),
        '-c:v', 'libx264',
        '-preset', VIDEO_PRESET,
        '-crf', str(VIDEO_CRF),
        '-pix_fmt', 'yuv420p',
        '-g', str(CAPTURE_FPS),
        '-keyint_min', str(max(1, CAPTURE_FPS // 2)),
        '-sc_threshold', '0',
    ]


def set_chart_prepare_status(msg: str) -> None:
    global _prepare_status
    with _prepare_status_lock:
        _prepare_status = msg or ''


def get_chart_prepare_status() -> str:
    with _prepare_status_lock:
        return _prepare_status


def request_chart_batch_cancel() -> None:
    _batch_cancel.set()


def _reset_chart_batch_cancel() -> None:
    _batch_cancel.clear()


def chart_preview_dir() -> Path:
    """优先包内构建产物，其次配置 static 目录。"""
    bundled = _PKG_ROOT / 'static' / 'chart_preview'
    if (bundled / 'index.html').is_file():
        return bundled
    try:
        from ..config import static as cfg_static

        alt = Path(cfg_static) / 'chart_preview'
        if (alt / 'index.html').is_file():
            return alt
    except Exception:
        pass
    return bundled


def _lock_for(key: str) -> asyncio.Lock:
    if key not in _BUILD_LOCKS:
        _BUILD_LOCKS[key] = asyncio.Lock()
    return _BUILD_LOCKS[key]


def preview_song_id(music_id: str, music_type: str) -> str:
    """与「谱面」指令一致：DX 曲库 ID 去掉前缀 1。"""
    mid = str(music_id).strip()
    if music_type == 'DX' and mid.startswith('1') and len(mid) > 1:
        return mid[1:]
    return mid


def chart_kind(music_type: str) -> str:
    return 'standard' if music_type == 'SD' else 'dx'


def chart_file_id(song_id: str, kind: str) -> int:
    n = int(song_id)
    if kind == 'utage':
        return n
    if kind == 'dx' and n < 100000:
        return n + 10000
    return n


def pick_chart_diff(level_count: int) -> int:
    """优先白/紫/红，返回 Chart Preview 的 diff（2–6）。"""
    for idx in (4, 3, 2, 1, 0):
        if idx < level_count:
            return idx + 2
    return 5


def cache_key(music_id: str, kind: str, diff: int) -> str:
    return f'{music_id}_{kind}_{diff}_r{CHART_VIDEO_REV}'


def video_path_for(music_id: str, kind: str, diff: int) -> Path:
    return CHART_GUESS_CACHE_DIR / cache_key(music_id, kind, diff) / 'chart.mp4'


def bgm_video_path_for(music_id: str, kind: str, diff: int) -> Path:
    return CHART_GUESS_CACHE_DIR / cache_key(music_id, kind, diff) / 'chart_bgm.mp4'


def is_chart_video_ready(music_id: str, kind: str, diff: int) -> bool:
    path = video_path_for(music_id, kind, diff)
    return path.is_file() and path.stat().st_size > 1024


def is_chart_bgm_ready(music_id: str, kind: str, diff: int) -> bool:
    path = bgm_video_path_for(music_id, kind, diff)
    return path.is_file() and path.stat().st_size > 1024


def is_chart_round_ready(music_id: str, kind: str, diff: int) -> bool:
    """阶段1 + 阶段2 均就绪。"""
    return is_chart_video_ready(music_id, kind, diff) and is_chart_bgm_ready(music_id, kind, diff)


def _load_manifest() -> dict:
    if not CHART_GUESS_MANIFEST.exists():
        return {'version': CHART_VIDEO_REV, 'entries': {}}
    try:
        return json.loads(CHART_GUESS_MANIFEST.read_text(encoding='utf-8'))
    except Exception:
        return {'version': CHART_VIDEO_REV, 'entries': {}}


def _save_manifest(data: dict) -> None:
    CHART_GUESS_DIR.mkdir(parents=True, exist_ok=True)
    CHART_GUESS_MANIFEST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def get_chart_manifest_entry(music_id: str, kind: str, diff: int) -> dict:
    key = cache_key(music_id, kind, diff)
    return (_load_manifest().get('entries') or {}).get(key, {})


async def chart_simai_exists(song_id: str, kind: str) -> bool:
    fid = chart_file_id(song_id, kind)
    url = f'{DEFAULT_CHART_CDN}/{fid}.txt'
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # 部分 CDN 对 HEAD 不友好，优先短 GET
            resp = await client.get(url, headers={'Range': 'bytes=0-64'})
            if resp.status_code < 400 and resp.content:
                return True
            resp = await client.get(url)
            return resp.status_code < 400 and bool(resp.text.strip())
    except Exception as e:
        log.warning(f'[GuessChart] 探测谱面失败 id={fid}: {e}')
        return False


def _ensure_static_server() -> int:
    global _static_server, _static_port
    with _static_lock:
        if _static_server is not None and _static_port is not None:
            return _static_port

        root = chart_preview_dir()
        if not (root / 'index.html').is_file():
            raise FileNotFoundError(
                f'未找到谱面预览静态页：{root}\n'
                '请先执行：cd chart_preview && npm install --legacy-peer-deps && npm run build'
            )

        class _Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(root), **kwargs)

            def log_message(self, fmt: str, *args) -> None:
                return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()

        server = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _static_server = server
        _static_port = port
        log.info(f'[GuessChart] 静态服务已启动 http://127.0.0.1:{port}/ → {root}')
        return port


def _ffprobe_duration(path: Path) -> float:
    proc = _run_cmd(
        [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=nw=1:nk=1',
            str(path),
        ],
        low_priority=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'ffprobe 失败: {proc.stderr[-400:]}')
    try:
        duration = float(proc.stdout.strip())
        if math.isfinite(duration) and duration > 0:
            return max(0.1, duration)
    except ValueError:
        pass

    # Chromium MediaRecorder 生成的 WebM 通常没有容器 duration；取末包即可。
    packet_proc = _run_cmd(
        [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'packet=pts_time,duration_time',
            '-of', 'json',
            str(path),
        ],
        low_priority=True,
    )
    if packet_proc.returncode != 0:
        raise RuntimeError(f'ffprobe packet 失败: {packet_proc.stderr[-400:]}')
    try:
        packet_data = json.loads(packet_proc.stdout)
        packets = (
            packet_data.get('packets') or []
            if isinstance(packet_data, dict) else []
        )
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        raise RuntimeError('ffprobe packet 输出无效') from e
    max_end = 0.0
    for packet in packets:
        try:
            pts = float(packet.get('pts_time'))
            packet_duration = float(packet.get('duration_time') or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(pts) and math.isfinite(packet_duration):
            max_end = max(max_end, pts + max(0.0, packet_duration))
    if max_end > 0:
        return max(0.1, max_end)
    raise RuntimeError(f'ffprobe 时长无效: {proc.stdout!r}')


def _decode_video_base64(payload: object) -> bytes:
    """严格解码录制视频，并把传输损坏变成可定位错误。"""
    encoded = str(payload or '').strip()
    if encoded.startswith('data:') and ',' in encoded:
        encoded = encoded.split(',', 1)[1]
    encoded = ''.join(encoded.split())
    if not encoded:
        raise RuntimeError('页面未返回录制视频（videoBase64 为空）')
    remainder = len(encoded) % 4
    if remainder == 1:
        raise RuntimeError(f'录制视频 Base64 长度损坏（chars={len(encoded)}）')
    if remainder:
        encoded += '=' * (4 - remainder)
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f'录制视频 Base64 解码失败（chars={len(encoded)}）') from e


def _answer_wav_path() -> Path:
    return chart_preview_dir() / 'assets' / 'maimai' / 'chart' / 'answer.wav'


def _load_wav_mono_pcm16(path: Path) -> Tuple[int, List[int]]:
    with wave.open(str(path), 'rb') as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise RuntimeError(f'answer.wav 需为 16-bit PCM，实际 sampwidth={width}')
    samples = array('h')
    samples.frombytes(frames)
    if channels == 2:
        samples = array(
            'h',
            (int((samples[i] + samples[i + 1]) / 2) for i in range(0, len(samples) - 1, 2)),
        )
    elif channels != 1:
        raise RuntimeError(f'不支持的声道数: {channels}')
    return rate, list(samples)


def _build_answer_track_wav(
    out_wav: Path,
    *,
    duration_sec: float,
    hit_offsets_ms: Sequence[float],
) -> bool:
    """按击打时间铺正解音轨；失败返回 False（仍可输出无声视频）。"""
    answer_path = _answer_wav_path()
    if not answer_path.is_file():
        log.warning(f'[GuessChart] 未找到正解音文件: {answer_path}')
        return False
    try:
        rate, answer = _load_wav_mono_pcm16(answer_path)
    except Exception as e:
        log.warning(f'[GuessChart] 读取正解音失败: {e}')
        return False

    # 预留完整 answer 尾音，避免最后几击被截断
    total = max(1, int(round(duration_sec * rate)) + len(answer) + rate // 10)
    mix = array('h', [0]) * total
    hits = sorted({round(float(x), 3) for x in hit_offsets_ms if float(x) >= 0})
    if len(hits) > MAX_HIT_SOUNDS:
        # 均匀抽样，避免超密谱把音轨打爆
        step = len(hits) / MAX_HIT_SOUNDS
        hits = [hits[int(i * step)] for i in range(MAX_HIT_SOUNDS)]
    placed = 0
    for hit_ms in hits:
        start = int(round(hit_ms / 1000.0 * rate))
        if start >= total:
            continue
        for i, sample in enumerate(answer):
            idx = start + i
            if idx >= total:
                break
            val = int(mix[idx]) + int(sample * 0.9)
            mix[idx] = max(-32767, min(32767, val))
        placed += 1

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(mix.tobytes())
    log.info(f'[GuessChart] 正解音轨 hits={placed}/{len(hits)} duration={duration_sec:.3f}s')
    return placed > 0


def _encode_silent_mp4(webm: Path, silent: Path) -> float:
    silent.parent.mkdir(parents=True, exist_ok=True)
    cmd_video = [
        'ffmpeg', '-y',
        '-i', str(webm),
        '-an',
        *_ffmpeg_x264_args(),
        '-vf', f'scale={DEFAULT_VIEWPORT}:{DEFAULT_VIEWPORT},fps={CAPTURE_FPS}',
        '-r', str(CAPTURE_FPS),
        '-vsync', 'cfr',
        '-movflags', '+faststart',
        str(silent),
    ]
    proc = _run_cmd(cmd_video)
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg 视频转码失败: {proc.stderr[-800:]}')
    return _ffprobe_duration(silent)


def _encode_webm_with_audio(webm: Path, audio: Path, mp4: Path) -> None:
    """一次完成 VP8/VP9 -> H.264 与音频混流，避免画面重复编码。"""
    video_dur = _ffprobe_duration(webm)
    audio_dur = _ffprobe_duration(audio)
    pad = max(0.0, audio_dur - video_dur + 0.05)
    video_filter = (
        f'scale={DEFAULT_VIEWPORT}:{DEFAULT_VIEWPORT},fps={CAPTURE_FPS},'
        f'tpad=stop_mode=clone:stop_duration={pad:.3f}'
        if pad > 0.01 else
        f'scale={DEFAULT_VIEWPORT}:{DEFAULT_VIEWPORT},fps={CAPTURE_FPS}'
    )
    tmp = mp4.with_suffix('.tmp.mp4')
    cmd = [
        'ffmpeg', '-y',
        '-i', str(webm),
        '-i', str(audio),
        '-map', '0:v:0',
        '-map', '1:a:0',
        *_ffmpeg_x264_args(),
        '-vf', video_filter,
        '-r', str(CAPTURE_FPS),
        '-vsync', 'cfr',
        '-c:a', 'aac',
        '-b:a', AUDIO_BITRATE,
        '-ar', '48000',
        '-shortest',
        '-movflags', '+faststart',
        str(tmp),
    ]
    proc = _run_cmd(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg 单次编码混音失败: {proc.stderr[-800:]}')
    tmp.replace(mp4)


def _ffmpeg_remux_embedded(webm: Path, mp4: Path) -> None:
    """webm 已含音画同轨时，高质量转码为 mp4。"""
    mp4.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp4.with_suffix('.tmp.mp4')
    cmd = [
        'ffmpeg', '-y',
        '-i', str(webm),
        *_ffmpeg_x264_args(),
        '-vf', f'scale={DEFAULT_VIEWPORT}:{DEFAULT_VIEWPORT},fps={CAPTURE_FPS}',
        '-r', str(CAPTURE_FPS),
        '-vsync', 'cfr',
        '-c:a', 'aac',
        '-b:a', AUDIO_BITRATE,
        '-ar', '48000',
        '-threads', str(FFMPEG_THREADS),
        '-movflags', '+faststart',
        str(tmp),
    ]
    proc = _run_cmd(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg 同轨转码失败: {proc.stderr[-800:]}')
    tmp.replace(mp4)


def _ffmpeg_mux(
    webm: Path,
    mp4: Path,
    *,
    hit_offsets_ms: Sequence[float],
    record_lead_ms: float = 0.0,
    nominal_duration: float = DEFAULT_DURATION,
) -> None:
    """将 MediaRecorder webm 转码，并按录制时间轴混入正解音。"""
    mp4.parent.mkdir(parents=True, exist_ok=True)
    work = mp4.parent / '_encode'
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    video_dur = _ffprobe_duration(webm)
    lead = max(0.0, float(record_lead_ms))
    lead_sec = lead / 1000.0
    # 按实际画面播放段 / 名义时长等比映射 hit，补偿录制时钟漂移
    play_video = max(0.05, video_dur - lead_sec)
    nominal = max(0.05, float(nominal_duration))
    scale = play_video / nominal
    aligned_hits = [lead + float(h) * scale for h in hit_offsets_ms]

    audio_wav = work / 'answers.wav'
    has_audio = _build_answer_track_wav(
        audio_wav,
        duration_sec=video_dur + 0.35,
        hit_offsets_ms=aligned_hits,
    )
    if has_audio:
        _encode_webm_with_audio(webm, audio_wav, mp4)
    else:
        _encode_silent_mp4(webm, mp4)
    shutil.rmtree(work, ignore_errors=True)


def _music_cdn_urls(music_id: str) -> List[str]:
    """原曲 mp3 URL 候选（与猜曲子一致的 ID 回落）。"""
    try:
        from .maimaidx_guess_audio import cdn_url_candidates

        return cdn_url_candidates(music_id)
    except ImportError:
        pass
    base = 'https://assets2.lxns.net/maimai/music'
    ordered: List[str] = []
    seen = set()

    def add(sid: str) -> None:
        if sid and sid not in seen:
            seen.add(sid)
            ordered.append(sid)

    add(str(music_id).strip())
    try:
        n = int(music_id)
    except (TypeError, ValueError):
        return [f'{base}/{ordered[0]}.mp3'] if ordered else []
    if n >= 10000:
        add(str(n - 10000))
    if n >= 11000:
        add(str(n - 11000))
    sid = str(music_id)
    if sid.startswith('1') and len(sid) > 1:
        add(sid[1:])
    return [f'{base}/{sid}.mp3' for sid in ordered]


def _download_music_mp3(music_id: str, dest: Path) -> None:
    """从 Lxns CDN 下载原曲 mp3。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        for url in _music_cdn_urls(music_id):
            try:
                resp = client.get(url)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                if not resp.content:
                    continue
                dest.write_bytes(resp.content)
                log.info(
                    f'[GuessChart] BGM 下载完成 music={music_id} '
                    f'size={len(resp.content) // 1024}KB url={url}'
                )
                return
            except Exception as e:
                last_err = e
    raise RuntimeError(f'CDN 无可用 BGM (music_id={music_id}): {last_err}')


def _ffmpeg_mux_bgm(
    webm: Path,
    mp4: Path,
    *,
    source_mp3: Path,
    music_start_sec: float,
    duration_sec: float,
    record_lead_ms: float = 0.0,
) -> None:
    """静音谱面 + 原曲裁剪（music_start_sec 已扣 lead-in）；adelay 对齐录制前置。"""
    mp4.parent.mkdir(parents=True, exist_ok=True)
    work = mp4.parent / '_encode_bgm'
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    video_dur = _ffprobe_duration(webm)
    lead_ms = max(0, int(round(float(record_lead_ms))))
    lead_sec = lead_ms / 1000.0
    play_video = max(0.5, video_dur - lead_sec)
    # 音频长度贴合实际画面播放段，减少尾部错位
    clip_dur = max(0.5, min(float(duration_sec) + 0.05, play_video + 0.05))

    clip = work / 'bgm_clip.m4a'
    cmd_clip = [
        'ffmpeg', '-y',
        '-ss', f'{max(0.0, float(music_start_sec)):.3f}',
        '-t', f'{clip_dur:.3f}',
        '-i', str(source_mp3),
        '-af', f'adelay={lead_ms}|{lead_ms},aresample=48000',
        '-c:a', 'aac',
        '-b:a', AUDIO_BITRATE,
        str(clip),
    ]
    proc = _run_cmd(cmd_clip)
    if proc.returncode != 0:
        raise RuntimeError(f'ffmpeg BGM 裁剪失败: {proc.stderr[-800:]}')

    _encode_webm_with_audio(webm, clip, mp4)
    shutil.rmtree(work, ignore_errors=True)


async def _capture_record_page(
    *,
    song_id: str,
    kind: str,
    diff: int,
    duration: int,
    tail: Optional[int] = None,
    with_audio: bool = False,
) -> dict:
    """打开录制页并返回 bridge meta（含 videoBase64）。"""
    port = await asyncio.to_thread(_ensure_static_server)
    q: Dict[str, str] = {
        'song': song_id,
        'kind': kind,
        'diff': str(diff),
        'duration': str(duration),
        'start': '-1',
        'hispeed': '6',
    }
    if tail and tail > 0:
        q['tail'] = str(int(tail))
        q['duration'] = str(int(tail))
    if with_audio:
        q['withAudio'] = '1'
    query = urlencode(q)
    url = f'http://127.0.0.1:{port}/#/record?{query}'

    meta: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding',
                '--disable-backgrounding-occluded-windows',
                '--autoplay-policy=no-user-gesture-required',
            ],
        )
        # Chromium 默认 nice=0，会与 bot 抢核；降到与 ffmpeg 同级
        await asyncio.to_thread(_renice_playwright_chromium, CHROMIUM_NICE)
        context = await browser.new_context(
            viewport={'width': DEFAULT_VIEWPORT, 'height': DEFAULT_VIEWPORT},
            device_scale_factor=1,
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_function(
                """() => window.__GUESS_CHART__
                    && ['ready','playing','done','error'].includes(window.__GUESS_CHART__.state)""",
                timeout=90000,
            )
            bridge = await page.evaluate('() => window.__GUESS_CHART__')
            if bridge.get('state') == 'error':
                raise RuntimeError(bridge.get('error') or '谱面加载失败')
            await page.wait_for_function(
                """() => window.__GUESS_CHART__
                    && (window.__GUESS_CHART__.state === 'done'
                        || window.__GUESS_CHART__.state === 'error')""",
                timeout=(duration + 90) * 1000,
            )
            bridge = await page.evaluate(
                """() => {
                    const g = window.__GUESS_CHART__ || {};
                    return {
                        state: g.state,
                        error: g.error,
                        durationSec: g.durationSec,
                        startSec: g.startSec,
                        songId: g.songId,
                        kind: g.kind,
                        diff: g.diff,
                        hitOffsetsMs: g.hitOffsetsMs || [],
                        recordLeadMs: g.recordLeadMs || 0,
                        musicStartSec: g.musicStartSec || 0,
                        hasEmbeddedAudio: !!g.hasEmbeddedAudio,
                        videoBase64Length: (g.videoBase64 || '').length,
                        videoMime: g.videoMime || null,
                    };
                }"""
            )
            if bridge.get('state') == 'error':
                raise RuntimeError(bridge.get('error') or '谱面录制失败')
            meta = dict(bridge or {})
            try:
                base64_length = int(meta.pop('videoBase64Length', 0) or 0)
            except (TypeError, ValueError):
                base64_length = 0
            if base64_length <= 0:
                raise RuntimeError('页面未返回录制视频（videoBase64 为空）')
            chunks: List[str] = []
            for start in range(0, base64_length, BASE64_CHUNK_CHARS):
                end = min(base64_length, start + BASE64_CHUNK_CHARS)
                chunk = await page.evaluate(
                    """({start, end}) => {
                        const value = window.__GUESS_CHART__?.videoBase64 || '';
                        return value.slice(start, end);
                    }""",
                    {'start': start, 'end': end},
                )
                if not isinstance(chunk, str) or len(chunk) != end - start:
                    raise RuntimeError(
                        f'录制视频 Base64 分块传输损坏（{start}:{end}）'
                    )
                chunks.append(chunk)
            meta['videoBase64'] = ''.join(chunks)
        finally:
            await page.close()
            await context.close()
            await browser.close()

    return meta


async def _render_chart_video(
    *,
    song_id: str,
    kind: str,
    diff: int,
    out_mp4: Path,
    duration: int = DEFAULT_DURATION,
    tail: Optional[int] = None,
    music_id: Optional[str] = None,
    mix_bgm: bool = False,
) -> dict:
    async with _get_render_sem():
        work = out_mp4.parent / ('_work_bgm' if mix_bgm else '_work')
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        dl_task: Optional[asyncio.Task] = None
        src = work / 'source.mp3'
        try:
            # BGM：浏览器内 withAudio 在 headless 下易因 audio 加载挂起；
            # 统一静音录制曲末谱面，再用 CDN mp3 混流（稳定、可预制）。
            if mix_bgm and music_id:
                dl_task = asyncio.create_task(
                    _to_cpu(_download_music_mp3, music_id, src)
                )

            meta = await _capture_record_page(
                song_id=song_id,
                kind=kind,
                diff=diff,
                duration=duration,
                tail=tail,
                with_audio=False,
            )
            webm = work / 'capture.webm'
            webm.write_bytes(_decode_video_base64(meta.get('videoBase64')))
            hits = meta.get('hitOffsetsMs') or []
            if not isinstance(hits, list):
                hits = []
            try:
                lead_ms = float(meta.get('recordLeadMs') or 0)
            except (TypeError, ValueError):
                lead_ms = 0.0
            try:
                start_sec = float(meta.get('startSec') or 0)
            except (TypeError, ValueError):
                start_sec = 0.0
            try:
                music_start_sec = float(meta.get('musicStartSec') or start_sec)
            except (TypeError, ValueError):
                music_start_sec = start_sec

            if mix_bgm:
                if not music_id:
                    raise RuntimeError('BGM 混流需要 music_id')
                if dl_task is not None:
                    await dl_task
                elif not src.is_file():
                    await _to_cpu(_download_music_mp3, music_id, src)
                await _to_cpu(
                    _ffmpeg_mux_bgm,
                    webm,
                    out_mp4,
                    source_mp3=src,
                    music_start_sec=music_start_sec,
                    duration_sec=float(tail or duration),
                    record_lead_ms=lead_ms,
                )
            else:
                if dl_task is not None:
                    dl_task.cancel()
                    try:
                        await dl_task
                    except (asyncio.CancelledError, Exception):
                        pass
                await _to_cpu(
                    _ffmpeg_mux,
                    webm,
                    out_mp4,
                    hit_offsets_ms=hits,
                    record_lead_ms=lead_ms,
                    nominal_duration=float(duration),
                )

            meta.pop('videoBase64', None)
            meta['hit_count'] = len(hits)
            meta['recordLeadMs'] = lead_ms
            meta['startSec'] = start_sec
            meta['musicStartSec'] = music_start_sec
            meta['hasEmbeddedAudio'] = False
            return meta
        finally:
            if dl_task is not None and not dl_task.done():
                dl_task.cancel()
                try:
                    await dl_task
                except (asyncio.CancelledError, Exception):
                    pass
            shutil.rmtree(work, ignore_errors=True)


def _entry_with_paths(
    music_id: str,
    kind: str,
    diff: int,
    *,
    song_id: str,
    title: str,
    duration: int,
    out: Path,
    out_bgm: Optional[Path],
    meta: dict,
    meta_bgm: Optional[dict],
    elapsed: int,
) -> dict:
    entry = {
        'music_id': str(music_id),
        'song_id': song_id,
        'kind': kind,
        'diff': diff,
        'diff_name': CHART_DIFF_NAMES.get(diff, str(diff)),
        'duration': duration,
        'bgm_duration': PHASE2_DURATION,
        'start_sec': meta.get('startSec'),
        'path': str(out.resolve()),
        'path_bgm': str(out_bgm.resolve()) if out_bgm and out_bgm.is_file() else '',
        'has_bgm': bool(out_bgm and out_bgm.is_file()),
        'size': out.stat().st_size,
        'size_bgm': out_bgm.stat().st_size if out_bgm and out_bgm.is_file() else 0,
        'rev': CHART_VIDEO_REV,
        'built_at': int(time.time()),
        'elapsed_sec': elapsed,
        'title': title,
        'bgm_start_sec': (meta_bgm or {}).get('startSec'),
    }
    return entry


async def _ensure_bgm_for_ready_mute(
    music_id: str,
    *,
    kind: str,
    diff: int,
    song_id: str,
    entry: dict,
    render_bgm: bool = True,
) -> dict:
    """静音已就绪时补齐 BGM；失败保留静音并打明确日志。"""
    key = cache_key(music_id, kind, diff)
    out_bgm = bgm_video_path_for(music_id, kind, diff)
    if is_chart_bgm_ready(music_id, kind, diff):
        entry = dict(entry)
        entry.setdefault('path_bgm', str(out_bgm.resolve()))
        entry['has_bgm'] = True
        entry.setdefault('bgm_duration', PHASE2_DURATION)
        return entry
    if not render_bgm:
        entry = dict(entry)
        entry['has_bgm'] = False
        entry['path_bgm'] = ''
        return entry

    async with _lock_for(key + '_bgm'):
        if is_chart_bgm_ready(music_id, kind, diff):
            entry = dict(entry)
            entry['path_bgm'] = str(out_bgm.resolve())
            entry['has_bgm'] = True
            entry.setdefault('bgm_duration', PHASE2_DURATION)
            return entry
        try:
            set_chart_prepare_status('补渲染曲末 BGM 谱面…')
            log.info(
                f'[GuessChart] 补渲染 BGM music={music_id} kind={kind} diff={diff}'
            )
            meta_bgm = await _render_chart_video(
                song_id=song_id,
                kind=kind,
                diff=diff,
                out_mp4=out_bgm,
                duration=PHASE2_DURATION,
                tail=PHASE2_DURATION,
                music_id=str(music_id),
                mix_bgm=True,
            )
            if not is_chart_bgm_ready(music_id, kind, diff):
                raise RuntimeError('BGM 文件未生成或过小')
            entry = dict(entry)
            entry['music_id'] = str(music_id)
            entry['kind'] = kind
            entry['diff'] = diff
            entry['path'] = str(video_path_for(music_id, kind, diff).resolve())
            entry['path_bgm'] = str(out_bgm.resolve())
            entry['has_bgm'] = True
            entry['size_bgm'] = out_bgm.stat().st_size
            entry['bgm_duration'] = PHASE2_DURATION
            entry['bgm_start_sec'] = meta_bgm.get('startSec')
            entry['rev'] = CHART_VIDEO_REV
            manifest = _load_manifest()
            manifest.setdefault('entries', {})[key] = entry
            _save_manifest(manifest)
            log.info(
                f'[GuessChart] BGM 补渲染成功 music={music_id} '
                f'size_bgm={entry["size_bgm"]}'
            )
            return entry
        except Exception as e:
            log.warning(
                f'[GuessChart] BGM 补渲染失败 music={music_id} '
                f'kind={kind} diff={diff}: {type(e).__name__}: {e}'
            )
            entry = dict(entry)
            entry['has_bgm'] = False
            entry['path_bgm'] = ''
            return entry


async def ensure_chart_video_ready(
    music_id: str,
    *,
    music_type: str,
    title: str = '',
    level_count: int = 5,
    duration: int = DEFAULT_DURATION,
    render_bgm: bool = True,
    prefer_kind: Optional[str] = None,
    prefer_diff: Optional[int] = None,
) -> Tuple[bool, str, Optional[Path], dict]:
    """确保缓存中有阶段1视频；尽力同时准备阶段2 BGM 视频。"""
    primary_kind = prefer_kind or chart_kind(music_type)
    song_id = preview_song_id(music_id, music_type)
    diff = int(prefer_diff) if prefer_diff is not None else pick_chart_diff(level_count)
    kind_candidates = [primary_kind]
    alt = 'standard' if primary_kind == 'dx' else 'dx'
    if alt not in kind_candidates:
        kind_candidates.append(alt)

    last_err = ''
    set_chart_prepare_status('探测谱面资源…')
    for kind in kind_candidates:
        key = cache_key(music_id, kind, diff)
        out = video_path_for(music_id, kind, diff)
        out_bgm = bgm_video_path_for(music_id, kind, diff)

        if is_chart_video_ready(music_id, kind, diff):
            entry = get_chart_manifest_entry(music_id, kind, diff) or {
                'music_id': str(music_id),
                'kind': kind,
                'diff': diff,
                'path': str(out.resolve()),
                'duration': duration,
            }
            entry = await _ensure_bgm_for_ready_mute(
                music_id,
                kind=kind,
                diff=diff,
                song_id=song_id,
                entry=entry,
                render_bgm=render_bgm,
            )
            set_chart_prepare_status(
                '命中完整缓存' if entry.get('has_bgm') else '命中静音缓存'
            )
            return True, 'cache', out, entry

        async with _lock_for(key):
            if is_chart_video_ready(music_id, kind, diff):
                entry = get_chart_manifest_entry(music_id, kind, diff) or {
                    'music_id': str(music_id),
                    'kind': kind,
                    'diff': diff,
                    'path': str(out.resolve()),
                }
                entry = await _ensure_bgm_for_ready_mute(
                    music_id,
                    kind=kind,
                    diff=diff,
                    song_id=song_id,
                    entry=entry,
                    render_bgm=render_bgm,
                )
                set_chart_prepare_status(
                    '命中完整缓存' if entry.get('has_bgm') else '命中静音缓存'
                )
                return True, 'cache', out, entry

            if not await chart_simai_exists(song_id, kind):
                last_err = f'CDN 无谱面（{song_id}/{kind}）'
                continue

            log.info(
                f'[GuessChart] 开始并行渲染 music={music_id} title={title!r} '
                f'song={song_id} kind={kind} diff={diff} '
                f'duration={duration}s+{PHASE2_DURATION}s '
                f'workers={RENDER_WORKERS}'
            )
            started = time.time()
            set_chart_prepare_status(
                f'并行录制静音谱面与曲末 BGM（约 {max(duration, PHASE2_DURATION)}s）…'
            )
            mute_task = asyncio.create_task(
                _render_chart_video(
                    song_id=song_id,
                    kind=kind,
                    diff=diff,
                    out_mp4=out,
                    duration=duration,
                )
            )
            bgm_task = asyncio.create_task(
                _render_chart_video(
                    song_id=song_id,
                    kind=kind,
                    diff=diff,
                    out_mp4=out_bgm,
                    duration=PHASE2_DURATION,
                    tail=PHASE2_DURATION,
                    music_id=str(music_id),
                    mix_bgm=True,
                )
            )
            mute_res, bgm_res = await asyncio.gather(
                mute_task, bgm_task, return_exceptions=True,
            )
            if not isinstance(mute_res, dict):
                last_err = str(mute_res)
                log.warning(f'[GuessChart] 渲染失败 music={music_id} kind={kind}: {mute_res}')
                if out_bgm.is_file():
                    out_bgm.unlink(missing_ok=True)
                continue

            meta = mute_res
            meta_bgm: Optional[dict] = None
            bgm_ok: Optional[Path] = None
            if not isinstance(bgm_res, dict):
                log.warning(
                    f'[GuessChart] BGM 渲染失败 music={music_id}: '
                    f'{type(bgm_res).__name__}: {bgm_res}'
                )
            else:
                meta_bgm = bgm_res
                if is_chart_bgm_ready(music_id, kind, diff):
                    bgm_ok = out_bgm
                else:
                    log.warning(
                        f'[GuessChart] BGM 渲染返回成功但文件无效 music={music_id}'
                    )

            elapsed = int(time.time() - started)
            entry = _entry_with_paths(
                music_id, kind, diff,
                song_id=song_id,
                title=title,
                duration=duration,
                out=out,
                out_bgm=bgm_ok,
                meta=meta,
                meta_bgm=meta_bgm,
                elapsed=elapsed,
            )
            manifest = _load_manifest()
            manifest.setdefault('entries', {})[key] = entry
            _save_manifest(manifest)
            set_chart_prepare_status('渲染完成')
            log.info(
                f'[GuessChart] 渲染完成 music={music_id} size={entry["size"]} '
                f'bgm={entry["has_bgm"]} elapsed={elapsed}s '
                f'(parallel mute+bgm)'
            )
            return True, 'built', out, entry

    set_chart_prepare_status(last_err or '无可用谱面')
    return False, last_err or '无可用谱面', None, {}


def list_ready_chart_music_ids() -> List[str]:
    manifest = _load_manifest()
    ids = []
    for entry in (manifest.get('entries') or {}).values():
        mid = str(entry.get('music_id') or '')
        path = Path(entry.get('path') or '')
        if mid and path.is_file():
            ids.append(mid)
    return ids


def list_ready_chart_rounds() -> List[Tuple[str, str, int]]:
    """已具备静音+BGM 的 (music_id, kind, diff)。"""
    ready: List[Tuple[str, str, int]] = []
    if not CHART_GUESS_CACHE_DIR.is_dir():
        return ready
    for d in CHART_GUESS_CACHE_DIR.iterdir():
        if not d.is_dir():
            continue
        m = _CACHE_KEY_RE.match(d.name)
        if not m:
            continue
        mid, kind, diff = m.group('mid'), m.group('kind'), int(m.group('diff'))
        if is_chart_round_ready(mid, kind, diff):
            ready.append((mid, kind, diff))
    return ready


def list_mute_without_bgm() -> List[Tuple[str, str, int]]:
    """静音已缓存、BGM 缺失的条目（后台补洞优先）。"""
    holes: List[Tuple[str, str, int]] = []
    if not CHART_GUESS_CACHE_DIR.is_dir():
        return holes
    for d in CHART_GUESS_CACHE_DIR.iterdir():
        if not d.is_dir():
            continue
        m = _CACHE_KEY_RE.match(d.name)
        if not m:
            continue
        mid, kind, diff = m.group('mid'), m.group('kind'), int(m.group('diff'))
        if is_chart_video_ready(mid, kind, diff) and not is_chart_bgm_ready(mid, kind, diff):
            holes.append((mid, kind, diff))
    return holes


def cleanup_stale_chart_workdirs() -> int:
    """清理失败残留的 _work / _work_bgm。"""
    removed = 0
    if not CHART_GUESS_CACHE_DIR.is_dir():
        return 0
    for d in CHART_GUESS_CACHE_DIR.iterdir():
        if not d.is_dir():
            continue
        for name in ('_work', '_work_bgm', '_encode', '_encode_bgm'):
            junk = d / name
            if junk.exists():
                shutil.rmtree(junk, ignore_errors=True)
                removed += 1
    if removed:
        log.info(f'[GuessChart] 清理残留工作目录 {removed} 个')
    return removed


async def fill_missing_chart_bgm(
    *,
    limit: int = 20,
    music_type_lookup: Optional[Dict[str, str]] = None,
) -> Tuple[int, int]:
    """补渲染缺失 BGM。返回 (成功, 失败)。"""
    if BG_FILL_WORKERS <= 0:
        return 0, 0
    holes = list_mute_without_bgm()
    if not holes:
        return 0, 0
    todo = holes[: max(1, min(int(limit), BG_FILL_WORKERS))]
    ok_n = fail_n = 0
    log.info(
        f'[GuessChart] 开始补 BGM 空洞 {len(todo)}/{len(holes)} '
        f'bg_fill_workers={BG_FILL_WORKERS} load={_system_load_ratio():.2f}'
    )

    async def _one(mid: str, kind: str, diff: int) -> bool:
        async with _get_bg_fill_sem():
            if _batch_cancel.is_set():
                return False
            music_type = 'DX' if kind == 'dx' else 'SD'
            if music_type_lookup and mid in music_type_lookup:
                music_type = music_type_lookup[mid]
            song_id = preview_song_id(mid, music_type)
            entry = get_chart_manifest_entry(mid, kind, diff) or {
                'music_id': mid, 'kind': kind, 'diff': diff,
            }
            try:
                new_entry = await _ensure_bgm_for_ready_mute(
                    mid,
                    kind=kind,
                    diff=diff,
                    song_id=song_id,
                    entry=entry,
                    render_bgm=True,
                )
                return bool(new_entry.get('has_bgm'))
            except Exception as e:
                log.warning(
                    f'[GuessChart] 后台补 BGM 异常 {mid}/{kind}/{diff}: {e}'
                )
                return False

    results = await asyncio.gather(
        *[_one(mid, kind, diff) for mid, kind, diff in todo],
        return_exceptions=True,
    )
    for item in results:
        if item is True:
            ok_n += 1
        else:
            fail_n += 1
    log.info(f'[GuessChart] 补 BGM 本轮完成 ok={ok_n} fail={fail_n}')
    return ok_n, fail_n


async def _chart_bgm_background_fill_loop() -> None:
    await asyncio.sleep(BG_FILL_STARTUP_DELAY_SEC)
    cleanup_stale_chart_workdirs()
    log.info(
        f'[GuessChart] 后台补 BGM 已启动 workers={BG_FILL_WORKERS} '
        f'adaptive={int(ADAPTIVE_ENABLED)} '
        f'load_pause>={BG_FILL_LOAD_RATIO:.2f} '
        f'(延迟 {BG_FILL_STARTUP_DELAY_SEC}s；忙时停、闲时由自适应升并发)'
    )
    while True:
        try:
            if BG_FILL_WORKERS <= 0:
                # 自适应关闭补洞时短睡，便于闲时尽快恢复
                await asyncio.sleep(
                    ADAPTIVE_INTERVAL_SEC if ADAPTIVE_ENABLED else BG_FILL_IDLE_SLEEP_SEC
                )
                continue
            if _batch_cancel.is_set() or _bg_fill_should_pause():
                await asyncio.sleep(
                    ADAPTIVE_INTERVAL_SEC if ADAPTIVE_ENABLED else BG_FILL_IDLE_SLEEP_SEC
                )
                continue
            holes = await asyncio.to_thread(list_mute_without_bgm)
            if not holes:
                await asyncio.sleep(BG_FILL_IDLE_SLEEP_SEC)
                continue
            ok_n, _fail_n = await fill_missing_chart_bgm(limit=BG_FILL_WORKERS)
            await asyncio.sleep(
                BG_FILL_BUSY_SLEEP_SEC if ok_n > 0 else BG_FILL_IDLE_SLEEP_SEC
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f'[GuessChart] 后台补 BGM 循环异常: {e}')
            await asyncio.sleep(BG_FILL_IDLE_SLEEP_SEC)


def schedule_chart_adaptive_controller() -> None:
    """启动自适应并发控制器（单例）。关闭自适应时为 no-op。"""
    global _adaptive_task
    if not ADAPTIVE_ENABLED:
        log.info('[GuessChart] MAIMAIDX_CHART_ADAPTIVE=0，使用固定并发')
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning('[GuessChart] 无事件循环，跳过自适应并发')
        return
    if _adaptive_task is not None and not _adaptive_task.done():
        return
    _adaptive_task = loop.create_task(
        _chart_adaptive_loop(),
        name='maimaidx-chart-adaptive',
    )


def schedule_chart_cache_background_fill() -> None:
    """启动后小并发补齐 BGM 空洞；可重复调用（单例）。

    固定模式且 BG_FILL=0 时不启动；自适应模式始终启动循环（workers=0 时休眠）。
    """
    global _bg_fill_task
    schedule_chart_adaptive_controller()
    if not ADAPTIVE_ENABLED and BG_FILL_WORKERS <= 0:
        log.info('[GuessChart] MAIMAIDX_CHART_BG_FILL_WORKERS=0，跳过后台补 BGM')
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning('[GuessChart] 无事件循环，跳过后台补 BGM')
        return
    if _bg_fill_task is not None and not _bg_fill_task.done():
        return
    _bg_fill_task = loop.create_task(
        _chart_bgm_background_fill_loop(),
        name='maimaidx-chart-bgm-fill',
    )


async def build_hot_chart_cache(
    *,
    force: bool = False,
    limit: Optional[int] = None,
) -> str:
    """烘焙热门池猜铺面视频（含曲末 BGM）。limit 限制本次新建/重建数量。"""
    from .maimaidx_music import guess, mai

    _reset_chart_batch_cancel()
    cleanup_stale_chart_workdirs()
    if not mai.total_list:
        return '曲库未加载，请等待 bot 初始化完成后再试。'
    pool = guess._guess_music_pool()
    if not pool:
        return '热门池为空，无法烘焙。'

    build_limit = DEFAULT_CHART_BATCH_LIMIT if limit is None else max(1, int(limit))
    if force and limit is None:
        build_limit = len(pool)

    ok_ids: List[str] = []
    skip_ids: List[str] = []
    fail_lines: List[str] = []
    bgm_filled = 0
    cancelled = False
    t0 = time.time()

    # 优先补已有静音缺 BGM 的空洞（不依赖随机 diff）
    type_lookup = {str(m.id): m.type for m in pool}
    if not force:
        set_chart_prepare_status('优先补齐已有缓存的曲末 BGM…')
        fill_budget = min(build_limit, max(BG_FILL_WORKERS * 4, 8))
        bgm_ok, bgm_fail = await fill_missing_chart_bgm(
            limit=fill_budget,
            music_type_lookup=type_lookup,
        )
        bgm_filled = bgm_ok
        build_limit = max(0, build_limit - bgm_ok)
        if bgm_fail:
            fail_lines.append(f'补 BGM 失败 {bgm_fail} 首（详见日志）')

    # 再收集热门池待建列表
    todo: List = []
    for music in pool:
        if build_limit <= 0:
            break
        mid = str(music.id)
        kind = chart_kind(music.type)
        diff = pick_chart_diff(len(music.ds))
        alt = 'standard' if kind == 'dx' else 'dx'
        if not force and (
            is_chart_round_ready(mid, kind, diff) or is_chart_round_ready(mid, alt, diff)
        ):
            skip_ids.append(mid)
            continue
        todo.append(music)
        if len(todo) >= build_limit:
            break

    song_sem = _get_batch_song_sem()
    done_count = 0
    done_lock = asyncio.Lock()
    batch_started = time.perf_counter()
    initial_seconds_per_song = _env_int('MAIMAIDX_CHART_ESTIMATE_SECONDS', 180)
    initial_eta = len(todo) * initial_seconds_per_song / max(1, BATCH_SONG_WORKERS)

    set_chart_prepare_status(
        f'热门池预制开始（待建 {len(todo)}，并发曲目 {BATCH_SONG_WORKERS}，'
        f'录制槽 {RENDER_WORKERS}）…'
    )
    log.info(
        f'[GuessChart] 热门池并行预制 todo={len(todo)} skip={len(skip_ids)} '
        f'bgm_filled={bgm_filled} batch_songs={BATCH_SONG_WORKERS} '
        f'render_workers={RENDER_WORKERS} cpu_pool={CPU_POOL_WORKERS} '
        f'ffmpeg_threads={FFMPEG_THREADS} cpu={_cpu_count()} '
        f'estimated_total={_format_duration(initial_eta)}'
    )

    async def _build_one(music, idx: int) -> Tuple[str, bool, str, dict]:
        nonlocal cancelled, done_count
        mid = str(music.id)
        kind = chart_kind(music.type)
        diff = pick_chart_diff(len(music.ds))
        alt = 'standard' if kind == 'dx' else 'dx'
        async with song_sem:
            if _batch_cancel.is_set():
                cancelled = True
                return mid, False, '烘焙任务已取消', {}
            async with done_lock:
                set_chart_prepare_status(
                    f'预制并发中 {done_count}/{len(todo)} '
                    f'（槽位 {BATCH_SONG_WORKERS} 曲 × 录制 {RENDER_WORKERS}）…'
                )
            log.info(
                f'[GuessChart] 热门池预制 [{idx}/{len(todo)}] {mid} {music.title} '
                f'force={force}'
            )
            try:
                if force:
                    for k in (kind, alt):
                        for p in (
                            video_path_for(mid, k, diff),
                            bgm_video_path_for(mid, k, diff),
                        ):
                            if p.is_file():
                                p.unlink()
                ok, msg, _path, entry = await ensure_chart_video_ready(
                    mid,
                    music_type=music.type,
                    title=music.title,
                    level_count=len(music.ds),
                    render_bgm=True,
                )
                # 仅静音不算完整成功：避免烧尽配额却永远无 BGM
                if ok and not (isinstance(entry, dict) and entry.get('has_bgm')):
                    ok = False
                    msg = msg + '（缺 BGM）'
            except asyncio.CancelledError:
                request_chart_batch_cancel()
                cancelled = True
                raise
            except Exception as e:
                ok, msg, entry = False, str(e), {}
            async with done_lock:
                done_count += 1
                elapsed_now = time.perf_counter() - batch_started
                remaining = max(0, len(todo) - done_count)
                eta = elapsed_now / done_count * remaining
                estimated_total = elapsed_now + eta
                progress = (done_count / len(todo) * 100.0) if todo else 100.0
                progress_msg = (
                    f'预制谱面 {done_count}/{len(todo)} ({progress:.1f}%) '
                    f'elapsed={_format_duration(elapsed_now)} ETA={_format_duration(eta)} '
                    f'estimated_total={_format_duration(estimated_total)} '
                    f'batch={song_sem.capacity}/{BATCH_SONG_MAX} '
                    f'render={RENDER_WORKERS}/{RENDER_MAX}'
                )
                set_chart_prepare_status(progress_msg)
                log.info(f'[GuessChart] {progress_msg}')
            return mid, ok, msg, entry if isinstance(entry, dict) else {}

    results = await asyncio.gather(
        *[_build_one(m, i) for i, m in enumerate(todo, 1)],
        return_exceptions=True,
    )
    for item, music in zip(results, todo):
        if isinstance(item, BaseException):
            if isinstance(item, asyncio.CancelledError):
                cancelled = True
            fail_lines.append(f'{music.id} {music.title}: {item}')
            continue
        mid, ok, msg, entry = item
        if ok:
            ok_ids.append(mid)
            log.info(f'[GuessChart] 预制成功 {mid} {msg} 有BGM')
        elif msg == '烘焙任务已取消':
            cancelled = True
        else:
            fail_lines.append(f'{mid} {music.title}: {msg}')
            log.warning(f'[GuessChart] 预制失败 {mid}: {msg}')

    elapsed = int(time.time() - t0)
    holes_left = len(list_mute_without_bgm())
    ready_n = len(list_ready_chart_rounds())
    set_chart_prepare_status('预制结束')
    lines = [
        f'猜铺面热门池预制完成（rev={CHART_VIDEO_REV}）',
        f'扫描 {len(pool)} 首，补 BGM {bgm_filled}，新建完整 {len(ok_ids)}，'
        f'跳过 {len(skip_ids)}，失败 {len(fail_lines)}，耗时 {elapsed}s',
        f'当前完整缓存 {ready_n}，仍缺 BGM {holes_left}',
        f'并发：曲目×{BATCH_SONG_WORKERS} / 录制槽×{RENDER_WORKERS} / '
        f'ffmpeg池×{CPU_POOL_WORKERS} / 单路线程×{FFMPEG_THREADS} / '
        f'后台补洞×{BG_FILL_WORKERS}（CPU {_cpu_count()}，nice={FFMPEG_NICE}）',
        f'上限相关；增量默认每次最多 {DEFAULT_CHART_BATCH_LIMIT} 首，'
        f'可用「更新猜铺面 50」或「更新猜铺面 -full」调整。',
    ]
    if cancelled:
        lines.append('（任务已取消）')
    if ok_ids[:15]:
        lines.append('成功：' + ', '.join(ok_ids[:15]) + ('…' if len(ok_ids) > 15 else ''))
    if fail_lines[:8]:
        lines.append('失败示例：')
        lines.extend(fail_lines[:8])
    return '\n'.join(lines)
