"""猜曲子：从 Lxns CDN 拉取试听、分轨混音并缓存阶段音频。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from loguru import logger as log

from .maimaidx_render_tasks import (
    finish_task,
    pending_tasks,
    start_task,
    update_task,
)

_PKG_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CDN_BASE = 'https://assets2.lxns.net/maimai/music'
STAGE_COUNT = 4
STAGE_DURATION = 30
STAGE_INTERVAL = 20
STAGE_FINAL_GRACE = 60
# 分轨前先从原曲裁一段，降低 CPU/内存压力（整首 demucs 易 OOM）
SEPARATION_CLIP_DURATION = STAGE_DURATION + 15
# htdemucs 有效 segment 上限约 7.8s，CLI 只接受整数故用 7
DEMUCS_SEGMENT = 7
STAGE_LABELS = ('仅鼓点', '鼓点 + 贝斯', '加入伴奏', '完整混音')
# demucs 四阶段分别混入的轨（第 4 阶段为全轨含人声，不再用原曲片段）
DEMUCS_STAGE_STEMS: Tuple[Tuple[str, ...], ...] = (
    ('drums',),
    ('drums', 'bass'),
    ('drums', 'bass', 'other'),
    ('drums', 'bass', 'other', 'vocals'),
)
# 混音逻辑变更时递增，使旧缓存自动失效
STAGE_MIX_REV = 2

AUDIO_GUESS_DIR = _PKG_ROOT / 'data' / 'audio_guess'
AUDIO_GUESS_CACHE_DIR = AUDIO_GUESS_DIR / 'cache'
AUDIO_GUESS_MANIFEST = AUDIO_GUESS_DIR / 'manifest.json'

_BUILD_LOCKS: Dict[str, asyncio.Lock] = {}
_active_subprocess: Optional[subprocess.Popen] = None
_batch_cancel = threading.Event()
_shutdown_hook_registered = False
_prepare_status = ''
_prepare_status_lock = threading.Lock()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _cpu_count() -> int:
    return max(1, int(os.cpu_count() or 4))


AUDIO_CPU_THREADS_MIN = _env_int('MAIMAIDX_AUDIO_CPU_THREADS_MIN', 2)
AUDIO_CPU_THREADS_MAX = _env_int(
    'MAIMAIDX_AUDIO_CPU_THREADS_MAX', min(4, max(2, _cpu_count() // 8)),
)
AUDIO_CPU_THREADS_MAX = max(AUDIO_CPU_THREADS_MIN, AUDIO_CPU_THREADS_MAX)
AUDIO_ESTIMATE_SECONDS = _env_int('MAIMAIDX_AUDIO_ESTIMATE_SECONDS', 150)


def _system_load_ratio() -> float:
    try:
        return float(os.getloadavg()[0]) / float(_cpu_count())
    except (AttributeError, OSError, ZeroDivisionError):
        return 0.0


def _dynamic_cpu_threads() -> int:
    """按整机 load 动态限制 Demucs/BLAS，始终为在线 Bot 留出大部分 CPU。"""
    load = _system_load_ratio()
    if load >= 0.50:
        return AUDIO_CPU_THREADS_MIN
    if load >= 0.30:
        return min(AUDIO_CPU_THREADS_MAX, AUDIO_CPU_THREADS_MIN + 1)
    return AUDIO_CPU_THREADS_MAX


def _format_duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, value = divmod(value, 3600)
    minutes, secs = divmod(value, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


def set_audio_prepare_status(msg: str) -> None:
    global _prepare_status
    with _prepare_status_lock:
        _prepare_status = msg or ''


def get_audio_prepare_status() -> str:
    with _prepare_status_lock:
        return _prepare_status


class GuessAudioCancelled(RuntimeError):
    """猜曲音频烘焙被用户或 bot 关闭中断。"""


def request_hot_batch_cancel() -> None:
    _batch_cancel.set()
    cancel_active_subprocess()


def _reset_hot_batch_cancel() -> None:
    _batch_cancel.clear()


def cancel_active_subprocess() -> None:
    global _active_subprocess
    proc = _active_subprocess
    if proc is None or proc.poll() is not None:
        return
    log.warning('[GuessAudio] 终止进行中的子进程…')
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _ensure_shutdown_hook() -> None:
    global _shutdown_hook_registered
    if _shutdown_hook_registered:
        return
    try:
        from nonebot import get_driver

        @get_driver().on_shutdown
        async def _on_guess_audio_shutdown() -> None:
            if _batch_cancel.is_set() or _active_subprocess is not None:
                log.info('[GuessAudio] bot 关闭，停止猜曲烘焙子进程')
            request_hot_batch_cancel()

        _shutdown_hook_registered = True
    except Exception:
        pass


def _cdn_base() -> str:
    try:
        from ..config import maiconfig
        return getattr(maiconfig, 'maimaidx_audio_cdn_base', None) or DEFAULT_CDN_BASE
    except Exception:
        return DEFAULT_CDN_BASE


def _lock_for(music_id: str) -> asyncio.Lock:
    if music_id not in _BUILD_LOCKS:
        _BUILD_LOCKS[music_id] = asyncio.Lock()
    return _BUILD_LOCKS[music_id]


def cdn_url_candidates(music_id: str) -> List[str]:
    """优先使用曲库新 ID，再尝试常见 CDN 回落 ID。"""
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
        return [f'{_cdn_base()}/{ordered[0]}.mp3'] if ordered else []

    if n >= 10000:
        add(str(n - 10000))
    if n >= 11000:
        add(str(n - 11000))
    sid = str(music_id)
    if sid.startswith('1') and len(sid) > 1:
        add(sid[1:])
    return [f'{_cdn_base()}/{sid}.mp3' for sid in ordered]


def _song_cache_dir(music_id: str) -> Path:
    return AUDIO_GUESS_CACHE_DIR / str(music_id)


def _stage_path(music_id: str, stage: int) -> Path:
    return _song_cache_dir(music_id) / f'stage_{stage:02d}.mp3'


def _load_manifest() -> Dict[str, dict]:
    if not AUDIO_GUESS_MANIFEST.exists():
        return {}
    try:
        return json.loads(AUDIO_GUESS_MANIFEST.read_text(encoding='utf-8'))
    except Exception as e:
        log.warning(f'[GuessAudio] manifest 读取失败: {e}')
        return {}


def _save_manifest(data: Dict[str, dict]) -> None:
    AUDIO_GUESS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_GUESS_MANIFEST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _stage_files_complete(music_id: str) -> bool:
    mid = str(music_id)
    return all(_stage_path(mid, i).is_file() for i in range(1, STAGE_COUNT + 1))


def _try_adopt_stale_cache(music_id: str) -> bool:
    """旧 mix_rev 或 manifest 缺失，但四段文件完整且校验通过时仅更新 manifest。"""
    mid = str(music_id)
    if not _stage_files_complete(mid):
        return False
    try:
        _verify_stage_audio(mid)
    except RuntimeError as e:
        log.debug(f'[GuessAudio] 无法复用旧缓存 music_id={mid}: {e}')
        return False
    manifest = _load_manifest()
    entry = manifest.get(mid, {})
    manifest[mid] = {
        **entry,
        'ready': True,
        'stages': STAGE_COUNT,
        'mix_rev': STAGE_MIX_REV,
    }
    _save_manifest(manifest)
    log.info(f'[GuessAudio] 复用已有阶段文件 music_id={mid} mix_rev->{STAGE_MIX_REV}')
    return True


def is_audio_ready(music_id: str) -> bool:
    mid = str(music_id)
    manifest = _load_manifest()
    entry = manifest.get(mid)
    if entry and entry.get('ready') and entry.get('mix_rev') == STAGE_MIX_REV:
        stages = int(entry.get('stages', STAGE_COUNT))
        if all(_stage_path(mid, i).is_file() for i in range(1, stages + 1)):
            return True
    if _try_adopt_stale_cache(mid):
        return True
    return False


def summarize_pool_cache(pool) -> Dict[str, int]:
    """统计热门池缓存状态（烘焙开始前打日志用）。"""
    stats = {'ready': 0, 'stale': 0, 'partial': 0, 'empty': 0}
    manifest = _load_manifest()
    for music in pool:
        mid = str(music.id)
        entry = manifest.get(mid)
        files = [_stage_path(mid, i).is_file() for i in range(1, STAGE_COUNT + 1)]
        complete = all(files)
        if entry and entry.get('ready') and entry.get('mix_rev') == STAGE_MIX_REV and complete:
            stats['ready'] += 1
        elif complete:
            stats['stale'] += 1
        elif any(files):
            stats['partial'] += 1
        else:
            stats['empty'] += 1
    return stats


def list_stage_files(music_id: str) -> List[Path]:
    mid = str(music_id)
    manifest = _load_manifest()
    stages = int((manifest.get(mid) or {}).get('stages', STAGE_COUNT))
    return [_stage_path(mid, i) for i in range(1, stages + 1)]


def _run(
    cmd: List[str], *, timeout: int = 600, cpu_threads: Optional[int] = None,
) -> None:
    global _active_subprocess
    if _batch_cancel.is_set():
        raise GuessAudioCancelled('烘焙任务已取消')
    threads = cpu_threads or _dynamic_cpu_threads()
    env = os.environ.copy()
    for name in (
        'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
        'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS',
    ):
        env[name] = str(threads)
    run_cmd = cmd if os.name == 'nt' else ['nice', '-n', '15', *cmd]
    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    _active_subprocess = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        if _batch_cancel.is_set():
            raise GuessAudioCancelled('烘焙任务已取消')
        if proc.returncode != 0:
            detail = (stderr or stdout or '').strip()
            tail = detail[-2000:] if detail else '(无 stderr/stdout)'
            log.error(f'[GuessAudio] 命令失败: {" ".join(cmd)}\n{tail}')
            raise RuntimeError(f'exit {proc.returncode}: {tail}')
    except subprocess.TimeoutExpired as e:
        proc.kill()
        proc.communicate()
        log.error(f'[GuessAudio] 命令超时 ({timeout}s): {" ".join(cmd)}')
        raise RuntimeError(f'超时 ({timeout}s)') from e
    finally:
        if _active_subprocess is proc:
            _active_subprocess = None


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(path),
        ],
        timeout=30,
    )
    return float(out.decode().strip())


def _pick_clip_offset(duration: float) -> float:
    if duration <= STAGE_DURATION + 2:
        return max(0.0, (duration - STAGE_DURATION) / 2)
    start = max(20.0, duration * 0.28)
    return min(start, max(0.0, duration - STAGE_DURATION - 3))


def get_audio_manifest_entry(music_id: str) -> dict:
    return _load_manifest().get(str(music_id), {})


def _file_digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _verify_stage_audio(music_id: str) -> None:
    paths = [_stage_path(music_id, i) for i in range(1, STAGE_COUNT + 1)]
    digests = [_file_digest(p) for p in paths]
    sizes = [p.stat().st_size for p in paths]
    log.info(
        f'[GuessAudio] 阶段校验 music_id={music_id} '
        f'sizes={sizes} digests={[d[:8] for d in digests]}'
    )
    if digests[0] == digests[-1]:
        raise RuntimeError('阶段 1 与阶段 4 音频相同，分轨无效')
    if len(set(digests)) < 2:
        raise RuntimeError('各阶段音频完全相同，分轨无效')


def _export_stem_mix(
    inputs: List[Path],
    output: Path,
    *,
    duration: int = STAGE_DURATION,
) -> None:
    """将 demucs 分轨按阶段混音并裁切为固定时长。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    n = len(inputs)
    chains = [
        f'[{i}:a]atrim=start=0:end={duration},asetpts=PTS-STARTPTS[a{i}]'
        for i in range(n)
    ]
    mix_inputs = ''.join(f'[a{i}]' for i in range(n))
    filt = (
        ';'.join(chains)
        + f';{mix_inputs}amix=inputs={n}:duration=longest:dropout_transition=0[out]'
    )
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error']
    for p in inputs:
        cmd += ['-i', str(p)]
    cmd += [
        '-filter_complex', filt,
        '-map', '[out]',
        '-ac', '2', '-ar', '44100', '-b:a', '128k',
        str(output),
    ]
    _run(cmd)


def _export_clip(
    inputs: List[Path],
    output: Path,
    *,
    offset: float,
    filters: Optional[str] = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(inputs) == 1 and filters:
        _run([
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-ss', f'{offset:.3f}', '-t', str(STAGE_DURATION),
            '-i', str(inputs[0]),
            '-filter_complex', filters,
            '-map', '[out]',
            '-ac', '2', '-ar', '44100', '-b:a', '128k',
            str(output),
        ])
        return
    if len(inputs) == 1 and not filters:
        _run([
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-ss', f'{offset:.3f}', '-t', str(STAGE_DURATION),
            '-i', str(inputs[0]),
            '-ac', '2', '-ar', '44100', '-b:a', '128k',
            str(output),
        ])
        return

    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error']
    for p in inputs:
        cmd += ['-i', str(p)]
    if filters:
        cmd += ['-filter_complex', filters, '-map', '[out]']
    else:
        ins = ''.join(f'[{i}:a]' for i in range(len(inputs)))
        cmd += [
            '-filter_complex',
            f'{ins}amix=inputs={len(inputs)}:duration=longest:dropout_transition=0[out]',
            '-map', '[out]',
        ]
    cmd += [
        '-ss', f'{offset:.3f}', '-t', str(STAGE_DURATION),
        '-ac', '2', '-ar', '44100', '-b:a', '128k',
        str(output),
    ]
    _run(cmd)


def _download_source_sync(music_id: str, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for url in cdn_url_candidates(music_id):
            try:
                resp = client.get(url)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                dest.write_bytes(resp.content)
                cdn_id = url.rsplit('/', 1)[-1].removesuffix('.mp3')
                size_kb = len(resp.content) // 1024
                log.info(
                    f'[GuessAudio] 下载完成 music_id={music_id} '
                    f'cdn_id={cdn_id} size={size_kb}KB url={url}'
                )
                return cdn_id
            except Exception as e:
                last_err = e
                log.debug(f'[GuessAudio] CDN 尝试失败 music_id={music_id} url={url}: {e}')
    raise RuntimeError(f'CDN 无可用音频 (music_id={music_id}): {last_err}')


def _demucs_available() -> bool:
    if not shutil.which('demucs'):
        return False
    try:
        import lameenc  # noqa: F401
    except ImportError:
        log.warning('[GuessAudio] demucs 已安装但缺少 lameenc，无法输出 mp3 分轨')
        return False
    return True


def _demucs_stem_paths(base: Path) -> Dict[str, Path]:
    """demucs 使用 --mp3 输出，避免 torchaudio 保存 wav 依赖 torchcodec。"""
    stems: Dict[str, Path] = {}
    for name in ('drums', 'bass', 'other', 'vocals'):
        for ext in ('.mp3', '.wav'):
            path = base / f'{name}{ext}'
            if path.is_file():
                stems[name] = path
                break
    return stems


def _extract_separation_clip(source: Path, clip: Path, offset: float) -> None:
    """从原曲截取短片段供 demucs 分轨，避免整首处理导致内存不足。"""
    log.info(
        f'[GuessAudio] 裁剪分轨片段 offset={offset:.2f}s '
        f'duration={SEPARATION_CLIP_DURATION}s -> {clip.name}'
    )
    clip.parent.mkdir(parents=True, exist_ok=True)
    _run([
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', f'{offset:.3f}', '-t', str(SEPARATION_CLIP_DURATION),
        '-i', str(source),
        '-ac', '2', '-ar', '44100',
        str(clip),
    ])


def _demucs_device() -> str:
    try:
        from ..config import maiconfig
        return getattr(maiconfig, 'maimaidx_demucs_device', None) or 'cpu'
    except Exception:
        return 'cpu'


def _separate_demucs(clip: Path, work_dir: Path) -> Dict[str, Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    device = _demucs_device()
    threads = _dynamic_cpu_threads()
    log.info(
        f'[GuessAudio] demucs 开始 model=htdemucs device={device} '
        f'segment={DEMUCS_SEGMENT} threads={threads}/{AUDIO_CPU_THREADS_MAX} '
        f'load={_system_load_ratio():.2f} output=mp3 input={clip.name}'
    )
    t0 = time.perf_counter()
    cmd = [
        'demucs',
        '-n', 'htdemucs',
        '-d', device,
        '--segment', str(DEMUCS_SEGMENT),
        '--mp3',
        '-o', str(work_dir),
        str(clip),
    ]
    _run(cmd, timeout=900, cpu_threads=threads)
    base = work_dir / 'htdemucs' / clip.stem
    stems = _demucs_stem_paths(base)
    missing = [k for k in ('drums', 'bass', 'other', 'vocals') if k not in stems]
    if missing:
        raise RuntimeError(f'Demucs 分轨不完整: {missing} (目录 {base})')
    elapsed = time.perf_counter() - t0
    log.info(f'[GuessAudio] demucs 完成 elapsed={elapsed:.1f}s output={base}')
    return stems


def _build_stages_demucs(source: Path, music_id: str, offset: float) -> None:
    log.info(f'[GuessAudio] demucs 分轨流程开始 music_id={music_id}')
    t0 = time.perf_counter()
    work_dir = _song_cache_dir(music_id) / '_work'
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    clip = work_dir / 'separation_clip.wav'
    _extract_separation_clip(source, clip, offset)
    stems = _separate_demucs(clip, work_dir)
    for stage_idx, names in enumerate(DEMUCS_STAGE_STEMS, 1):
        _export_stem_mix(
            [stems[name] for name in names],
            _stage_path(music_id, stage_idx),
        )
    _verify_stage_audio(music_id)
    shutil.rmtree(work_dir, ignore_errors=True)
    log.info(
        f'[GuessAudio] demucs 分轨流程完成 music_id={music_id} '
        f'elapsed={time.perf_counter() - t0:.1f}s'
    )


def _build_stages_ffmpeg(source: Path, music_id: str, offset: float) -> None:
    """无 Demucs 时用 EQ / 伴奏提取近似分轨（效果弱于 AI 分轨）。"""
    log.info(f'[GuessAudio] ffmpeg 近似分轨开始 music_id={music_id} offset={offset:.2f}s')
    t0 = time.perf_counter()
    s1 = _stage_path(music_id, 1)
    _export_clip(
        [source], s1, offset=offset,
        filters=(
            '[0:a]highpass=f=250,lowpass=f=2200,'
            'volume=2.5,alimiter=limit=0.95[out]'
        ),
    )
    s2 = _stage_path(music_id, 2)
    _export_clip(
        [source], s2, offset=offset,
        filters=(
            f'[0:a]lowpass=f=280,highpass=f=40,volume=1.6[dr];'
            f'[0:a]highpass=f=180,lowpass=f=3500,volume=1.4[hh];'
            f'[dr][hh]amix=inputs=2:duration=longest:dropout_transition=0,'
            f'alimiter=limit=0.95[out]'
        ),
    )
    s3 = _stage_path(music_id, 3)
    _export_clip(
        [source], s3, offset=offset,
        filters=(
            f'[0:a]pan=stereo|c0=c0-0.35*c1|c1=c1-0.35*c0,'
            f'highpass=f=80,alimiter=limit=0.95[out]'
        ),
    )
    _export_clip([source], _stage_path(music_id, 4), offset=offset)
    _verify_stage_audio(music_id)
    log.info(
        f'[GuessAudio] ffmpeg 近似分轨完成 music_id={music_id} '
        f'elapsed={time.perf_counter() - t0:.1f}s'
    )


def build_audio_cache_sync(
    music_id: str,
    *,
    title: str = '',
    force: bool = False,
) -> Tuple[bool, str]:
    """同步构建单首曲目的阶段音频缓存。供脚本或线程池调用。"""
    if shutil.which('ffmpeg') is None:
        return False, '服务器未安装 ffmpeg，无法处理音频'

    mid = str(music_id)
    if _batch_cancel.is_set():
        return False, '烘焙任务已取消'

    if not force and is_audio_ready(mid):
        log.debug(f'[GuessAudio] 跳过已缓存 music_id={mid}')
        return True, '已缓存'

    label = f' {title}' if title else ''
    log.info(f'[GuessAudio] 开始构建 music_id={mid}{label} force={force}')
    t0 = time.perf_counter()
    set_audio_prepare_status('准备音频…')

    cache_dir = _song_cache_dir(mid)
    cache_dir.mkdir(parents=True, exist_ok=True)
    source = cache_dir / 'source.mp3'

    try:
        set_audio_prepare_status('下载原曲…')
        cdn_id = _download_source_sync(mid, source)
    except RuntimeError as e:
        log.warning(f'[GuessAudio] 下载失败 music_id={mid}: {e}')
        set_audio_prepare_status('下载失败')
        return False, str(e)

    try:
        duration = _probe_duration(source)
        offset = _pick_clip_offset(duration)
        log.info(
            f'[GuessAudio] 源文件就绪 music_id={mid} '
            f'duration={duration:.1f}s clip_offset={offset:.2f}s cdn_id={cdn_id}'
        )
        mode = 'ffmpeg'
        if _demucs_available():
            try:
                set_audio_prepare_status('AI 分轨 demucs（约 1～3 分钟）…')
                _build_stages_demucs(source, mid, offset)
                mode = 'demucs'
            except Exception as demucs_err:
                err_text = str(demucs_err)
                if 'torchcodec' in err_text.lower():
                    log.warning(
                        '[GuessAudio] demucs 保存分轨需要 torchcodec 或 lameenc；'
                        '请 pip install lameenc 后重试，当前将回退 ffmpeg'
                    )
                log.warning(
                    f'[GuessAudio] demucs 分轨失败 music_id={mid}，回退 ffmpeg: {demucs_err}'
                )
                shutil.rmtree(cache_dir / '_work', ignore_errors=True)
                for i in range(1, STAGE_COUNT + 1):
                    p = _stage_path(mid, i)
                    if p.exists():
                        p.unlink()
                set_audio_prepare_status('回退 ffmpeg 分轨…')
                _build_stages_ffmpeg(source, mid, offset)
                mode = 'ffmpeg_fallback'
        else:
            log.warning('[GuessAudio] 未检测到 demucs，使用 ffmpeg 近似分轨')
            set_audio_prepare_status('ffmpeg 近似分轨…')
            _build_stages_ffmpeg(source, mid, offset)

        manifest = _load_manifest()
        manifest[mid] = {
            'ready': True,
            'stages': STAGE_COUNT,
            'mix_rev': STAGE_MIX_REV,
            'title': title,
            'cdn_id': cdn_id,
            'mode': mode,
            'clip_offset': round(offset, 2),
        }
        _save_manifest(manifest)
        elapsed = time.perf_counter() - t0
        set_audio_prepare_status('音频已就绪')
        log.info(
            f'[GuessAudio] 构建成功 music_id={mid}{label} mode={mode} '
            f'elapsed={elapsed:.1f}s'
        )
        return True, f'已生成 {STAGE_COUNT} 段 × {STAGE_DURATION}s（{mode}）'
    except GuessAudioCancelled as e:
        log.warning(f'[GuessAudio] 构建取消 music_id={mid}{label}: {e}')
        shutil.rmtree(cache_dir, ignore_errors=True)
        manifest = _load_manifest()
        manifest.pop(mid, None)
        _save_manifest(manifest)
        return False, '烘焙任务已取消'
    except Exception as e:
        log.exception(
            f'[GuessAudio] 构建失败 music_id={mid}{label} '
            f'elapsed={time.perf_counter() - t0:.1f}s: {e}'
        )
        shutil.rmtree(cache_dir, ignore_errors=True)
        manifest = _load_manifest()
        manifest.pop(mid, None)
        _save_manifest(manifest)
        return False, f'分轨失败: {e}'


async def ensure_audio_ready(music_id: str, *, title: str = '') -> Tuple[bool, str]:
    if is_audio_ready(music_id):
        return True, 'ready'
    log.info(f'[GuessAudio] 懒加载构建 music_id={music_id} title={title or "-"}')
    async with _lock_for(str(music_id)):
        if is_audio_ready(music_id):
            return True, 'ready'
        ok, msg = await asyncio.to_thread(
            build_audio_cache_sync, str(music_id), title=title,
        )
        if ok:
            log.info(f'[GuessAudio] 懒加载完成 music_id={music_id}: {msg}')
        else:
            log.warning(f'[GuessAudio] 懒加载失败 music_id={music_id}: {msg}')
        return ok, msg


def _format_hot_batch_report(
    pool_size: int,
    ok_ids: List[str],
    skip_ids: List[str],
    fail_lines: List[str],
    *,
    cancelled: bool = False,
) -> str:
    lines = []
    if cancelled:
        lines.append('猜曲音频烘焙已取消（已完成部分如下）。')
    lines.extend([
        f'猜曲音频烘焙完成（热门池共 {pool_size} 首）。',
        f'新建/重建：{len(ok_ids)}',
        f'已跳过（有缓存）：{len(skip_ids)}',
        f'失败：{len(fail_lines)}',
        '说明：增量模式会跳过 mix_rev 匹配或校验通过的旧文件；'
        '不是按上次中断序号续跑，而是扫描全池。',
    ])
    if fail_lines:
        preview = fail_lines[:8]
        lines.append('失败示例：')
        lines.extend(f'· {line}' for line in preview)
        if len(fail_lines) > 8:
            lines.append(f'… 另有 {len(fail_lines) - 8} 条')
    return '\n'.join(lines)


def _run_hot_batch_loop(
    pool,
    *,
    force: bool,
    build_one: Callable[[str, str], Tuple[bool, str]],
) -> Tuple[List[str], List[str], List[str], bool]:
    ok_ids: List[str] = []
    skip_ids: List[str] = []
    fail_lines: List[str] = []
    cancelled = False

    for idx, music in enumerate(pool, 1):
        if _batch_cancel.is_set():
            cancelled = True
            log.warning(f'[GuessAudio] 热门池烘焙取消于 {idx}/{len(pool)}')
            break
        mid = str(music.id)
        if not force and is_audio_ready(mid):
            skip_ids.append(mid)
            if idx % 50 == 0 or idx == len(pool):
                log.info(
                    f'[GuessAudio] 热门池进度 {idx}/{len(pool)} '
                    f'ok={len(ok_ids)} skip={len(skip_ids)} fail={len(fail_lines)}'
                )
            continue
        log.info(f'[GuessAudio] 热门池 [{idx}/{len(pool)}] 处理 {mid} {music.title}')
        ok, msg = build_one(mid, music.title)
        if ok:
            ok_ids.append(mid)
            log.info(f'[GuessAudio] 热门池 [{idx}/{len(pool)}] 成功 {mid}: {msg}')
        elif msg == '烘焙任务已取消':
            cancelled = True
            break
        else:
            fail_lines.append(f'{mid} {music.title}: {msg}')
            log.warning(f'[GuessAudio] 热门池 [{idx}/{len(pool)}] 失败 {mid}: {msg}')

    return ok_ids, skip_ids, fail_lines, cancelled


async def build_hot_audio_cache(*, force: bool = False) -> str:
    """异步烘焙热门池（逐首执行，支持 Ctrl+C / bot 关闭中断）。"""
    from .maimaidx_music import guess, mai

    _ensure_shutdown_hook()
    _reset_hot_batch_cancel()

    if not mai.total_list:
        return '曲库未加载，请等待 bot 初始化完成后再试。'
    pool = guess._guess_music_pool()
    if not pool:
        return '热门池为空，无法烘焙。'

    demucs_on = _demucs_available()
    cache_stats = summarize_pool_cache(pool)
    todo_count = len(pool) if force else sum(
        1 for music in pool if not is_audio_ready(str(music.id))
    )
    initial_estimate = todo_count * AUDIO_ESTIMATE_SECONDS
    task_id = start_task('audio', total=todo_count, force=force)
    log.info(
        f'[GuessAudio] 热门池烘焙开始 total={len(pool)} force={force} '
        f'demucs={"yes" if demucs_on else "no"} device={_demucs_device() if demucs_on else "-"} '
        f'cache_ready={cache_stats["ready"]} stale_files={cache_stats["stale"]} '
        f'partial={cache_stats["partial"]} empty={cache_stats["empty"]} mix_rev={STAGE_MIX_REV} '
        f'todo={todo_count} cpu_threads={AUDIO_CPU_THREADS_MIN}-{AUDIO_CPU_THREADS_MAX} '
        f'estimated_total={_format_duration(initial_estimate)}'
    )
    batch_t0 = time.perf_counter()

    async def _build_one(mid: str, title: str) -> Tuple[bool, str]:
        return await asyncio.to_thread(
            build_audio_cache_sync, mid, title=title, force=force,
        )

    ok_ids: List[str] = []
    skip_ids: List[str] = []
    fail_lines: List[str] = []
    cancelled = False

    for idx, music in enumerate(pool, 1):
        if _batch_cancel.is_set():
            cancelled = True
            log.warning(f'[GuessAudio] 热门池烘焙取消于 {idx}/{len(pool)}')
            break
        await asyncio.sleep(0)
        mid = str(music.id)
        if not force and is_audio_ready(mid):
            skip_ids.append(mid)
            if idx % 50 == 0 or idx == len(pool):
                log.info(
                    f'[GuessAudio] 热门池进度 {idx}/{len(pool)} '
                    f'ok={len(ok_ids)} skip={len(skip_ids)} fail={len(fail_lines)}'
                )
            continue
        set_audio_prepare_status(
            f'烘焙进度 {idx}/{len(pool)}（成功 {len(ok_ids)} / 跳过 {len(skip_ids)}）…'
        )
        log.info(f'[GuessAudio] 热门池 [{idx}/{len(pool)}] 处理 {mid} {music.title}')
        try:
            ok, msg = await _build_one(mid, music.title)
        except asyncio.CancelledError:
            request_hot_batch_cancel()
            cancelled = True
            log.warning(f'[GuessAudio] 热门池烘焙收到取消信号于 {idx}/{len(pool)}')
            break
        if ok:
            ok_ids.append(mid)
            log.info(f'[GuessAudio] 热门池 [{idx}/{len(pool)}] 成功 {mid}: {msg}')
        elif msg == '烘焙任务已取消':
            cancelled = True
            break
        else:
            fail_lines.append(f'{mid} {music.title}: {msg}')
            log.warning(f'[GuessAudio] 热门池 [{idx}/{len(pool)}] 失败 {mid}: {msg}')

        processed = len(ok_ids) + len(fail_lines)
        elapsed_now = time.perf_counter() - batch_t0
        remaining = max(0, todo_count - processed)
        eta = (elapsed_now / processed * remaining) if processed else initial_estimate
        estimated_total = elapsed_now + eta
        progress = (processed / todo_count * 100.0) if todo_count else 100.0
        progress_msg = (
            f'预制音频 {processed}/{todo_count} ({progress:.1f}%) '
            f'elapsed={_format_duration(elapsed_now)} ETA={_format_duration(eta)} '
            f'estimated_total={_format_duration(estimated_total)} '
            f'threads={_dynamic_cpu_threads()}/{AUDIO_CPU_THREADS_MAX}'
        )
        set_audio_prepare_status(progress_msg)
        update_task(
            task_id,
            processed=processed,
            total=todo_count,
            eta_seconds=eta,
            message=progress_msg,
        )
        log.info(f'[GuessAudio] {progress_msg}')

    elapsed = time.perf_counter() - batch_t0
    log.info(
        f'[GuessAudio] 热门池烘焙结束 total={len(pool)} '
        f'ok={len(ok_ids)} skip={len(skip_ids)} fail={len(fail_lines)} '
        f'cancelled={cancelled} elapsed={elapsed:.1f}s'
    )
    finish_task(task_id)
    return _format_hot_batch_report(
        len(pool), ok_ids, skip_ids, fail_lines, cancelled=cancelled,
    )


_render_recovery_task: Optional[asyncio.Task] = None
_auto_prepare_task: Optional[asyncio.Task] = None


async def _recover_audio_render_task(task: dict) -> None:
    await asyncio.sleep(8)
    try:
        await build_hot_audio_cache(force=bool(task.get('force')))
        log.info('[GuessAudio] 已自动恢复上次中断的音频预制任务')
        schedule_audio_cache_auto_prepare()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(f'[GuessAudio] 自动恢复音频预制失败：{type(exc).__name__}: {exc}')


def schedule_audio_render_recovery() -> None:
    """启动后恢复上次被进程中断的音频预制任务。"""
    global _render_recovery_task
    if _render_recovery_task is not None and not _render_recovery_task.done():
        return
    task = next((item for item in pending_tasks() if item.get('kind') == 'audio'), None)
    if task is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _render_recovery_task = loop.create_task(
        _recover_audio_render_task(task), name='maimaidx-audio-render-recovery'
    )


async def _auto_prepare_audio_cache() -> None:
    await asyncio.sleep(20)
    try:
        await build_hot_audio_cache()
        log.info('[GuessAudio] 启动后的增量音频预制完成')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning(f'[GuessAudio] 启动自动音频预制失败：{type(exc).__name__}: {exc}')


def schedule_audio_cache_auto_prepare() -> None:
    """每次启动自动检查热门池并增量预制缺失缓存。"""
    global _auto_prepare_task
    if _auto_prepare_task is not None and not _auto_prepare_task.done():
        return
    if any(item.get('kind') == 'audio' for item in pending_tasks()):
        schedule_audio_render_recovery()
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _auto_prepare_task = loop.create_task(
        _auto_prepare_audio_cache(), name='maimaidx-audio-auto-prepare'
    )


def build_hot_audio_cache_sync(*, force: bool = False) -> str:
    """同步烘焙热门池（供脚本调用）。"""
    from .maimaidx_music import guess, mai

    _reset_hot_batch_cancel()

    if not mai.total_list:
        return '曲库未加载，请等待 bot 初始化完成后再试。'
    pool = guess._guess_music_pool()
    if not pool:
        return '热门池为空，无法烘焙。'

    demucs_on = _demucs_available()
    cache_stats = summarize_pool_cache(pool)
    log.info(
        f'[GuessAudio] 热门池烘焙开始 total={len(pool)} force={force} '
        f'demucs={"yes" if demucs_on else "no"} device={_demucs_device() if demucs_on else "-"} '
        f'cache_ready={cache_stats["ready"]} stale_files={cache_stats["stale"]} '
        f'partial={cache_stats["partial"]} empty={cache_stats["empty"]} mix_rev={STAGE_MIX_REV}'
    )
    batch_t0 = time.perf_counter()

    ok_ids, skip_ids, fail_lines, cancelled = _run_hot_batch_loop(
        pool,
        force=force,
        build_one=lambda mid, title: build_audio_cache_sync(
            mid, title=title, force=force,
        ),
    )

    elapsed = time.perf_counter() - batch_t0
    log.info(
        f'[GuessAudio] 热门池烘焙结束 total={len(pool)} '
        f'ok={len(ok_ids)} skip={len(skip_ids)} fail={len(fail_lines)} '
        f'cancelled={cancelled} elapsed={elapsed:.1f}s'
    )
    return _format_hot_batch_report(
        len(pool), ok_ids, skip_ids, fail_lines, cancelled=cancelled,
    )
