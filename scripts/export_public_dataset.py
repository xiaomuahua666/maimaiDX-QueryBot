#!/usr/bin/env python3
"""从本地成绩快照导出脱敏公开数据集（排除「不同意共享」用户）。

用途：
- 生成 peer_stats（供锐评 ARPI / 同段样本）
- 导出匿名玩家快照与 Rating 趋势（供提示词 / 可行性优化）
- 生成 roast_training_samples.jsonl（锐评上下文骨架样本）
- 默认再打包 Hugging Face 合规 zip（无根目录 peer_stats 撞名）

用法示例：
  python scripts/export_public_dataset.py \\
    --output data/public_dataset \\
    --scores-dir data/user_scores \\
    --bucket-size 200 \\
    --min-bucket-players 8

输出目录主要文件：
  README.md
  dataset_meta.json
  peer_stats.json / peer_stats.json.gz
  players/{anon_id}.json
  rating_trends.jsonl
  roast_training_samples.jsonl
  hf_upload/                 # HF 合规目录
  ../hf_upload.zip           # 默认同级 zip，可直接上传
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import secrets
import shutil
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]


class _Progress:
    """简单终端进度：阶段名 + 百分比 + ETA，强制 flush 方便 SSH/nohup 实时看。"""

    def __init__(self, total: int, label: str, *, every: int = 25) -> None:
        self.total = max(0, int(total))
        self.label = label
        self.every = max(1, int(every))
        self.done = 0
        self.t0 = time.time()
        self._last_print = -1
        self._print(force=True)

    def _bar(self, ratio: float, width: int = 28) -> str:
        filled = int(width * ratio)
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    def _print(self, *, force: bool = False, suffix: str = "") -> None:
        if not force and self.done != self.total and (self.done - self._last_print) < self.every:
            return
        self._last_print = self.done
        elapsed = max(0.001, time.time() - self.t0)
        if self.total <= 0:
            line = f"[progress] {self.label}: {self.done}  elapsed={elapsed:.1f}s{suffix}"
        else:
            ratio = min(1.0, self.done / self.total)
            speed = self.done / elapsed
            remain = (self.total - self.done) / speed if speed > 0 else 0.0
            line = (
                f"[progress] {self.label}: {self._bar(ratio)} "
                f"{self.done}/{self.total} ({ratio * 100:5.1f}%) "
                f"speed={speed:.1f}/s ETA={remain:.0f}s elapsed={elapsed:.1f}s"
                f"{suffix}"
            )
        print(line, flush=True)

    def tick(self, n: int = 1, *, suffix: str = "") -> None:
        self.done += n
        self._print(suffix=suffix)

    def finish(self, *, suffix: str = "") -> None:
        if self.total > 0:
            self.done = self.total
        self._print(force=True, suffix=suffix)


def _log(msg: str) -> None:
    print(f"[export] {msg}", flush=True)


def _bootstrap_package() -> None:
    import types

    pkg_name = "nonebot_plugin_maimaidx"
    if pkg_name in sys.modules:
        return
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT)]
    sys.modules[pkg_name] = pkg
    lib = types.ModuleType(f"{pkg_name}.libraries")
    lib.__path__ = [str(ROOT / "libraries")]
    sys.modules[f"{pkg_name}.libraries"] = lib


_bootstrap_package()

from nonebot_plugin_maimaidx.libraries.maimaidx_data_share import (  # noqa: E402
    DataShareManager,
)
from nonebot_plugin_maimaidx.libraries.maimaidx_data_storage import (  # noqa: E402
    DATA_DIR as DEFAULT_SCORES_DIR,
    DataStorageManager,
    ScoreRecord,
)


def _percentile(sorted_vals: Sequence[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return float(d0 + d1)


def _anon_id(qqid: str, salt: str) -> str:
    digest = hashlib.sha256(f"maimaidx-share:{salt}:{qqid}".encode("utf-8")).hexdigest()
    return digest[:16]


def _resolve_salt(output_dir: Path, salt: Optional[str]) -> str:
    """优先：CLI/环境变量 → 已有 .dataset_salt（保证跨次导出 player_id 稳定）→ 新建。"""
    if salt:
        return salt.strip()
    env = (os.environ.get("MAIMAIDX_DATASET_SALT") or "").strip()
    if env:
        return env
    salt_file = Path(output_dir) / ".dataset_salt"
    if salt_file.exists():
        existing = salt_file.read_text(encoding="utf-8").strip()
        if existing:
            _log(f"reuse salt from {salt_file}")
            return existing
    return secrets.token_hex(16)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_opted_out(share_config: Path) -> set[str]:
    mgr = DataShareManager(share_config)
    return set(mgr.list_opted_out())


# song_id(str) -> is_new；由 --music-data 填充，优先于运行时曲库
_MUSIC_IS_NEW: Dict[str, bool] = {}


def load_music_is_new_map(path: Optional[Path]) -> Dict[str, bool]:
    """从 music_data / dxdata JSON 构建 is_new 映射，改善 B35/B15 划分。"""
    global _MUSIC_IS_NEW
    _MUSIC_IS_NEW = {}
    if not path:
        return _MUSIC_IS_NEW
    path = Path(path)
    if not path.exists():
        _log(f"music-data not found: {path}")
        return _MUSIC_IS_NEW
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"music-data load failed: {e}")
        return _MUSIC_IS_NEW

    items: list = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("data", "songs", "music", "list"):
            if isinstance(raw.get(key), list):
                items = raw[key]
                break
        if not items and all(isinstance(v, dict) for v in raw.values()):
            items = list(raw.values())

    out: Dict[str, bool] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id") or item.get("music_id")
        if sid is None:
            continue
        basic = item.get("basic_info") or item.get("basicInfo") or {}
        if not isinstance(basic, dict):
            basic = {}
        if "is_new" in item:
            is_new = bool(item.get("is_new"))
        elif "is_new" in basic:
            is_new = bool(basic.get("is_new"))
        elif "isNew" in item:
            is_new = bool(item.get("isNew"))
        else:
            continue
        out[str(sid)] = is_new
    _MUSIC_IS_NEW = out
    _log(f"music-data loaded: {len(out)} songs with is_new from {path}")
    return out


def _try_is_new_song(song_id: Any) -> Optional[bool]:
    """优先外部 music-data；其次运行时曲库；否则 None。"""
    sid = str(song_id)
    if sid in _MUSIC_IS_NEW:
        return _MUSIC_IS_NEW[sid]
    try:
        from nonebot_plugin_maimaidx.libraries.maimaidx_music import mai

        total = getattr(mai, "total_list", None)
        if not total:
            return None
        music = total.by_id(sid)
        if not music:
            return None
        from nonebot_plugin_maimaidx.libraries.maimaidx_best_50 import _song_is_new

        return bool(_song_is_new(song_id))
    except Exception:
        return None


def _build_b50(records: List[ScoreRecord]) -> Tuple[List[ScoreRecord], List[ScoreRecord], str]:
    """返回 (b35, b15, mode)；mode=version_split|top50_fallback。"""
    if not records:
        return [], [], "empty"
    flags = [_try_is_new_song(r.song_id) for r in records]
    known = sum(1 for f in flags if f is not None)
    if known < max(10, len(records) // 2):
        top = sorted(records, key=lambda x: int(x.ra), reverse=True)[:50]
        return top, [], "top50_fallback"
    b15 = sorted(
        [r for r, f in zip(records, flags) if f],
        key=lambda x: int(x.ra),
        reverse=True,
    )[:15]
    b35 = sorted(
        [r for r, f in zip(records, flags) if not f],
        key=lambda x: int(x.ra),
        reverse=True,
    )[:35]
    return b35, b15, "version_split"


def _sanitize_records(records: List[ScoreRecord]) -> Tuple[List[ScoreRecord], int]:
    """清洗非法成绩；返回 (合法列表, 丢弃数)。"""
    cleaned: List[ScoreRecord] = []
    dropped = 0
    seen: set[tuple[int, int]] = set()
    for r in records:
        try:
            song_id = int(r.song_id)
            level_index = int(r.level_index)
            ach = float(r.achievements)
            ds = float(r.ds)
            ra = int(r.ra)
        except (TypeError, ValueError):
            dropped += 1
            continue
        if song_id <= 0 or level_index < 0 or level_index > 4:
            dropped += 1
            continue
        if not (0.0 <= ach <= 101.0):
            dropped += 1
            continue
        if ds < 0 or ds > 15.5:
            dropped += 1
            continue
        if ra < 0 or ra > 5000:
            dropped += 1
            continue
        key = (song_id, level_index)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        cleaned.append(
            ScoreRecord(
                song_id=song_id,
                title=str(r.title or "")[:200],
                level=str(r.level or "")[:16],
                level_index=level_index,
                ds=ds,
                achievements=round(ach, 4),
                rate=str(r.rate or "")[:16],
                ra=ra,
                fc=(str(r.fc) if r.fc else None),
                fs=(str(r.fs) if r.fs else None),
                dxScore=max(0, int(r.dxScore or 0)),
            )
        )
    return cleaned, dropped


def _bucket_key(rating: int, size: int) -> str:
    lo = (int(rating) // size) * size
    return f"{lo}-{lo + size - 1}"


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _redact_record(r: ScoreRecord) -> dict:
    return {
        "song_id": int(r.song_id),
        "title": str(r.title or ""),
        "level": str(r.level or ""),
        "level_index": int(r.level_index),
        "ds": float(r.ds),
        "achievements": float(r.achievements),
        "rate": str(r.rate or ""),
        "ra": int(r.ra),
        "fc": r.fc or "",
        "fs": r.fs or "",
        "dxScore": int(r.dxScore or 0),
    }


def _iter_user_dirs(scores_dir: Path) -> Iterable[Path]:
    if not scores_dir.exists():
        return []
    return sorted(p for p in scores_dir.iterdir() if p.is_dir() and p.name.isdigit())


def _load_latest_snapshot(storage: DataStorageManager, qqid: int):
    metas = storage.list_snapshots(qqid, limit=1)
    if not metas:
        return None
    return storage.load_snapshot_by_id(qqid, str(metas[0].get("snapshot_id") or ""))


def _rating_trend(storage: DataStorageManager, qqid: int, days: int = 90) -> List[dict]:
    rows = storage.get_rating_history(qqid, days=days)
    # get_rating_history 新到旧；趋势导出改为旧到新
    out = []
    for m in reversed(rows):
        out.append(
            {
                "date": m.get("date"),
                "stored_at": m.get("stored_at"),
                "rating": int(m.get("rating") or 0),
                "record_count": int(m.get("record_count") or 0),
            }
        )
    return out


def _player_arpi(
    b50: Sequence[ScoreRecord],
    chart_avgs: Dict[str, float],
) -> Optional[float]:
    gaps = []
    for r in b50:
        key = f"{int(r.song_id)}:{int(r.level_index)}"
        avg = chart_avgs.get(key)
        if avg is None:
            continue
        gaps.append(float(r.achievements) - float(avg))
    if not gaps:
        return None
    return sum(gaps) / len(gaps)


def export_dataset(
    *,
    scores_dir: Path,
    share_config: Path,
    output_dir: Path,
    bucket_size: int = 200,
    min_bucket_players: int = 8,
    min_chart_samples: int = 3,
    trend_days: int = 90,
    salt: Optional[str] = None,
    include_full_records: bool = True,
    pack_hf: bool = True,
    hf_zip_path: Optional[Path] = None,
    music_data: Optional[Path] = None,
    min_records: int = 30,
    min_rating: int = 1000,
    min_b50: int = 20,
    min_trend_points: int = 0,
    require_trend: bool = False,
) -> dict:
    storage = DataStorageManager()
    storage_scores = scores_dir
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    opted_out = _load_opted_out(share_config)
    salt = _resolve_salt(output_dir, salt)
    load_music_is_new_map(music_data)

    players_dir = output_dir / "players"
    if players_dir.exists():
        shutil.rmtree(players_dir)
    players_dir.mkdir(parents=True, exist_ok=True)

    # 第一遍：收集可共享用户最新 B50，累计 chart 达成与出现次数
    bucket_charts: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {"sum_ach": 0.0, "n": 0, "b50_n": 0})
    )
    bucket_player_count: Dict[str, int] = defaultdict(int)
    player_payloads: List[dict] = []
    skip_reasons: Dict[str, int] = defaultdict(int)
    b50_mode_counts: Dict[str, int] = defaultdict(int)
    dropped_records_total = 0

    user_dirs = [p for p in _iter_user_dirs(storage_scores)]
    total_users = len(user_dirs)
    _log(
        f"phase1/scan users={total_users} opted_out={len(opted_out)} "
        f"scores_dir={storage_scores} "
        f"filters(min_records={min_records}, min_rating={min_rating}, "
        f"min_b50={min_b50}, min_trend={min_trend_points}, require_trend={require_trend})"
    )
    from nonebot_plugin_maimaidx.libraries import maimaidx_data_storage as _ds_mod

    original_dir = _ds_mod.DATA_DIR
    _ds_mod.DATA_DIR = storage_scores
    scan_prog = _Progress(total_users, "1/4 扫描存档", every=max(10, total_users // 50 or 1))
    try:
        for user_dir in user_dirs:
            qqid_s = user_dir.name
            suffix = (
                f" ok={len(player_payloads)} "
                f"skip={sum(skip_reasons.values())}"
            )
            if qqid_s in opted_out:
                skip_reasons["opt_out"] += 1
                scan_prog.tick(suffix=suffix)
                continue
            if not any(user_dir.iterdir()):
                skip_reasons["empty_dir"] += 1
                scan_prog.tick(suffix=suffix)
                continue

            qqid = int(qqid_s)
            try:
                snap = _load_latest_snapshot(storage, qqid)
            except Exception:
                skip_reasons["load_error"] += 1
                scan_prog.tick(suffix=suffix)
                continue
            if not snap or not snap.records:
                skip_reasons["no_snapshot"] += 1
                scan_prog.tick(suffix=suffix)
                continue

            records, dropped = _sanitize_records(list(snap.records))
            dropped_records_total += dropped
            if len(records) < min_records:
                skip_reasons["low_records"] += 1
                scan_prog.tick(suffix=suffix)
                continue

            rating = int(snap.rating or 0)
            if rating < min_rating:
                skip_reasons["low_rating"] += 1
                scan_prog.tick(suffix=suffix)
                continue

            b35, b15, b50_mode = _build_b50(records)
            b50 = b35 + b15
            if len(b50) < min_b50:
                skip_reasons["thin_b50"] += 1
                scan_prog.tick(suffix=suffix)
                continue
            b50_mode_counts[b50_mode] += 1

            trend = _rating_trend(storage, qqid, days=trend_days)
            if len(trend) < min_trend_points or (require_trend and len(trend) < 2):
                skip_reasons["thin_trend"] += 1
                scan_prog.tick(suffix=suffix)
                continue

            bkey = _bucket_key(rating, bucket_size)
            bucket_player_count[bkey] += 1
            seen_keys = set()
            for r in b50:
                ckey = f"{int(r.song_id)}:{int(r.level_index)}"
                if ckey in seen_keys:
                    continue
                seen_keys.add(ckey)
                cell = bucket_charts[bkey][ckey]
                cell["sum_ach"] += float(r.achievements)
                cell["n"] += 1
                cell["b50_n"] += 1

            anon = _anon_id(qqid_s, salt)
            delta = None
            if len(trend) >= 2:
                delta = int(trend[-1]["rating"]) - int(trend[0]["rating"])

            b50_ra = sum(int(r.ra) for r in b50)
            payload = {
                "schema_version": 2,
                "player_id": anon,
                "latest": {
                    "date": snap.date,
                    "stored_at": snap.stored_at,
                    "rating": rating,
                    "record_count": len(records),
                    "b35": [_redact_record(r) for r in b35],
                    "b15": [_redact_record(r) for r in b15],
                    "b50_mode": b50_mode,
                    "b50_ra_sum": b50_ra,
                    "rating_vs_b50_gap": rating - b50_ra,
                },
                "rating_trend": trend,
                "rating_delta": delta,
                "quality": {
                    "dropped_records": dropped,
                    "trend_points": len(trend),
                },
            }
            if include_full_records:
                payload["latest"]["records"] = [_redact_record(r) for r in records]
            player_payloads.append(payload)
            scan_prog.tick(
                suffix=f" ok={len(player_payloads)} skip={sum(skip_reasons.values())}"
            )
    finally:
        _ds_mod.DATA_DIR = original_dir
    scan_prog.finish(
        suffix=f" ok={len(player_payloads)} skip={sum(skip_reasons.values())}"
    )
    _log(
        f"phase1 done: usable={len(player_payloads)} "
        f"skips={dict(skip_reasons)} dropped_records={dropped_records_total} "
        f"b50_modes={dict(b50_mode_counts)}"
    )

    # 第二遍：用已聚合均值算每位玩家 ARPI，写入桶分布
    _log("phase2/compute ARPI + peer averages")
    chart_avg_by_bucket: Dict[str, Dict[str, float]] = {}
    for bkey, charts in bucket_charts.items():
        chart_avg_by_bucket[bkey] = {
            ckey: (cell["sum_ach"] / cell["n"]) if cell["n"] else 0.0
            for ckey, cell in charts.items()
        }

    bucket_arpi_values: Dict[str, List[float]] = defaultdict(list)
    arpi_prog = _Progress(
        len(player_payloads), "2/4 计算 ARPI", every=max(10, len(player_payloads) // 40 or 1)
    )
    for payload in player_payloads:
        rating = int(payload["latest"]["rating"])
        bkey = _bucket_key(rating, bucket_size)
        b50_recs = []
        for item in payload["latest"]["b35"] + payload["latest"]["b15"]:
            b50_recs.append(
                ScoreRecord(
                    song_id=item["song_id"],
                    title=item["title"],
                    level=item["level"],
                    level_index=item["level_index"],
                    ds=item["ds"],
                    achievements=item["achievements"],
                    rate=item["rate"],
                    ra=item["ra"],
                    fc=item.get("fc") or None,
                    fs=item.get("fs") or None,
                    dxScore=int(item.get("dxScore") or 0),
                )
            )
        arpi = _player_arpi(b50_recs, chart_avg_by_bucket.get(bkey, {}))
        payload["latest"]["arpi"] = None if arpi is None else round(arpi, 4)
        payload["latest"]["rating_bucket"] = bkey
        if arpi is not None:
            bucket_arpi_values[bkey].append(arpi)
        arpi_prog.tick()
    arpi_prog.finish()

    # 组装 peer_stats
    _log("phase3/build peer_stats buckets")
    buckets_out: Dict[str, Any] = {}
    bucket_keys = list(bucket_charts.keys())
    bucket_prog = _Progress(len(bucket_keys), "3/4 组装同段桶", every=1)
    for bkey in bucket_keys:
        charts = bucket_charts[bkey]
        player_n = bucket_player_count.get(bkey, 0)
        if player_n < min_bucket_players:
            bucket_prog.tick(suffix=f" kept={len(buckets_out)}")
            continue
        # 桶内人数很少时，谱面阈值降到不超过人数，避免小样本环境导出空 peer_stats
        chart_threshold = max(1, min(int(min_chart_samples), player_n))
        chart_stats = {}
        for ckey, cell in charts.items():
            if cell["n"] < chart_threshold:
                continue
            chart_stats[ckey] = {
                "avg_achievement": round(cell["sum_ach"] / cell["n"], 4),
                "sample_count": int(cell["n"]),
                "b50_appear_rate": round(cell["b50_n"] / player_n, 4),
            }
        if not chart_stats:
            bucket_prog.tick(suffix=f" kept={len(buckets_out)}")
            continue
        arpis = sorted(bucket_arpi_values.get(bkey) or [])
        dist = {
            "count": len(arpis),
            "mean": round(sum(arpis) / len(arpis), 4) if arpis else None,
            "median": None if not arpis else round(_percentile(arpis, 50) or 0.0, 4),
            "p25": None if not arpis else round(_percentile(arpis, 25) or 0.0, 4),
            "p75": None if not arpis else round(_percentile(arpis, 75) or 0.0, 4),
        }
        buckets_out[bkey] = {
            "player_count": player_n,
            "charts": chart_stats,
            "arpi_distribution": dist,
        }
        bucket_prog.tick(suffix=f" kept={len(buckets_out)}")
    bucket_prog.finish(suffix=f" kept={len(buckets_out)}")

    peer_stats = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rating_bucket_size": bucket_size,
        "min_bucket_players": min_bucket_players,
        "min_chart_samples": min_chart_samples,
        "source": "maimaidx-querybot-user-scores",
        "buckets": buckets_out,
    }

    # 写玩家文件与 jsonl
    _log(f"phase4/write files -> {output_dir}")
    trend_path = output_dir / "rating_trends.jsonl"
    roast_path = output_dir / "roast_training_samples.jsonl"
    write_prog = _Progress(
        len(player_payloads), "4/4 写出文件", every=max(10, len(player_payloads) // 40 or 1)
    )
    with open(trend_path, "w", encoding="utf-8") as f_trend, open(
        roast_path, "w", encoding="utf-8"
    ) as f_roast:
        for payload in player_payloads:
            anon = payload["player_id"]
            with open(players_dir / f"{anon}.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            trend_row = {
                "player_id": anon,
                "rating": payload["latest"]["rating"],
                "rating_bucket": payload["latest"]["rating_bucket"],
                "rating_delta": payload.get("rating_delta"),
                "trend": payload.get("rating_trend") or [],
                "arpi": payload["latest"].get("arpi"),
            }
            f_trend.write(json.dumps(trend_row, ensure_ascii=False) + "\n")

            # 锐评提示词优化用的轻量样本（无身份、含同段与趋势）
            sample = {
                "player_id": anon,
                "rating": payload["latest"]["rating"],
                "rating_bucket": payload["latest"]["rating_bucket"],
                "arpi": payload["latest"].get("arpi"),
                "rating_delta": payload.get("rating_delta"),
                "trend_points": len(payload.get("rating_trend") or []),
                "b35_count": len(payload["latest"]["b35"]),
                "b15_count": len(payload["latest"]["b15"]),
                "b35_top": [
                    {
                        "title": r["title"],
                        "ds": r["ds"],
                        "achievements": r["achievements"],
                        "ra": r["ra"],
                    }
                    for r in payload["latest"]["b35"][:5]
                ],
                "b15_top": [
                    {
                        "title": r["title"],
                        "ds": r["ds"],
                        "achievements": r["achievements"],
                        "ra": r["ra"],
                    }
                    for r in payload["latest"]["b15"][:5]
                ],
                "push_feasibility_hint": _feasibility_hint(payload),
            }
            f_roast.write(json.dumps(sample, ensure_ascii=False) + "\n")
            write_prog.tick()
    write_prog.finish()

    peer_json = output_dir / "peer_stats.json"
    peer_json.write_text(
        json.dumps(peer_stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with gzip.open(output_dir / "peer_stats.json.gz", "wt", encoding="utf-8") as gz:
        json.dump(peer_stats, gz, ensure_ascii=False)

    quality = {
        "scanned_user_dirs": total_users,
        "usable_players": len(player_payloads),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "dropped_records_total": dropped_records_total,
        "b50_mode_counts": dict(b50_mode_counts),
        "music_is_new_map_size": len(_MUSIC_IS_NEW),
        "filters": {
            "min_records": min_records,
            "min_rating": min_rating,
            "min_b50": min_b50,
            "min_trend_points": min_trend_points,
            "require_trend": require_trend,
        },
        "rating_stats": _summarize_ratings(player_payloads),
        "warnings": _quality_warnings(
            player_payloads, skip_reasons, b50_mode_counts, len(_MUSIC_IS_NEW)
        ),
    }

    meta = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "player_count": len(player_payloads),
        "skipped_opt_out": int(skip_reasons.get("opt_out", 0)),
        "skipped_empty": int(
            skip_reasons.get("empty_dir", 0) + skip_reasons.get("no_snapshot", 0)
        ),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "bucket_size": bucket_size,
        "min_bucket_players": min_bucket_players,
        "min_chart_samples": min_chart_samples,
        "trend_days": trend_days,
        "include_full_records": include_full_records,
        "bucket_count": len(buckets_out),
        "salt_fingerprint": hashlib.sha256(salt.encode()).hexdigest()[:12],
        "music_data": str(music_data) if music_data else "",
        "note": "player_id 为单向哈希，不可反查 QQ；opt-out 用户已排除。",
    }
    (output_dir / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "dataset_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "README.md").write_text(_readme_text(meta), encoding="utf-8")

    # 可选：把 salt 写到本地私密文件（勿公开）
    salt_path = output_dir / ".dataset_salt"
    salt_path.write_text(salt + "\n", encoding="utf-8")
    try:
        os.chmod(salt_path, 0o600)
    except OSError:
        pass

    if pack_hf:
        zip_path = pack_hf_upload(
            output_dir,
            zip_path=hf_zip_path,
            meta=meta,
        )
        meta["hf_upload_dir"] = str(output_dir / "hf_upload")
        meta["hf_upload_zip"] = str(zip_path)

    # 校验和（不含 salt / 私密文件）
    checksums = {}
    for rel in (
        "dataset_meta.json",
        "quality_report.json",
        "peer_stats.json",
        "peer_stats.json.gz",
        "rating_trends.jsonl",
        "roast_training_samples.jsonl",
        "README.md",
    ):
        p = output_dir / rel
        if p.exists():
            checksums[rel] = _sha256_file(p)
    if meta.get("hf_upload_zip") and Path(meta["hf_upload_zip"]).exists():
        checksums["hf_upload.zip"] = _sha256_file(Path(meta["hf_upload_zip"]))
    meta["checksums_sha256"] = checksums
    (output_dir / "checksums.sha256").write_text(
        "\n".join(f"{digest}  {name}" for name, digest in checksums.items()) + "\n",
        encoding="utf-8",
    )
    (output_dir / "dataset_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _log(
        f"done players={meta['player_count']} buckets={meta['bucket_count']} "
        f"output={output_dir}"
    )
    if quality.get("warnings"):
        for w in quality["warnings"]:
            _log(f"WARN: {w}")
    if meta.get("hf_upload_zip"):
        _log(f"hf_upload_zip={meta['hf_upload_zip']}")
    return meta


def _summarize_ratings(payloads: List[dict]) -> dict:
    ratings = [int((p.get("latest") or {}).get("rating") or 0) for p in payloads]
    if not ratings:
        return {}
    ratings_sorted = sorted(ratings)
    return {
        "count": len(ratings),
        "min": ratings_sorted[0],
        "max": ratings_sorted[-1],
        "mean": round(sum(ratings) / len(ratings), 2),
        "median": ratings_sorted[len(ratings_sorted) // 2],
    }


def _quality_warnings(
    payloads: List[dict],
    skip_reasons: Dict[str, int],
    b50_modes: Dict[str, int],
    music_map_size: int,
) -> List[str]:
    warnings: List[str] = []
    if not payloads:
        warnings.append("没有可用玩家样本")
    fallback = int(b50_modes.get("top50_fallback") or 0)
    split = int(b50_modes.get("version_split") or 0)
    if fallback and fallback >= max(1, split):
        warnings.append(
            "多数玩家使用 top50_fallback（未正确划分 B15）；建议提供 --music-data"
        )
    if music_map_size == 0:
        warnings.append("未加载 music-data is_new 映射，B15 划分可能不准确")
    empty = int(skip_reasons.get("empty_dir") or 0)
    if empty > len(payloads) * 3:
        warnings.append(
            f"空目录很多（{empty}），多为 mkdir 残留，不影响导出但可定期清理"
        )
    return warnings


def pack_hf_upload(
    output_dir: Path,
    *,
    zip_path: Optional[Path] = None,
    meta: Optional[dict] = None,
) -> Path:
    """打包 Hugging Face 合规目录与 zip。

    约束（避免 HF 建库失败）：
    - 根目录不放 peer_stats.json / peer_stats.json.gz
    - 只用 data/*.jsonl 作为 split；同段统计放 assets/arpi_peer_stats.json
    - 不附带数百个 players/*.json（改为单一 players.jsonl）
    - 不包含 .dataset_salt
    """
    output_dir = Path(output_dir)
    staging = output_dir / "hf_upload"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "data").mkdir(parents=True)
    (staging / "assets").mkdir(parents=True)

    if meta is None:
        meta_path = output_dir / "dataset_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    # jsonl splits
    for name in ("rating_trends.jsonl", "roast_training_samples.jsonl"):
        src = output_dir / name
        if src.exists():
            shutil.copy2(src, staging / "data" / name)

    players_dir = output_dir / "players"
    players_jl = staging / "data" / "players.jsonl"
    player_files = sorted(players_dir.glob("*.json")) if players_dir.exists() else []
    pack_prog = _Progress(
        len(player_files),
        "HF 打包 players.jsonl",
        every=max(10, len(player_files) // 40 or 1),
    )
    with open(players_jl, "w", encoding="utf-8") as w:
        for p in player_files:
            obj = json.loads(p.read_text(encoding="utf-8"))
            w.write(json.dumps(obj, ensure_ascii=False) + "\n")
            pack_prog.tick()
    pack_prog.finish()

    peer_src = output_dir / "peer_stats.json"
    if peer_src.exists():
        # 故意不用 peer_stats.json(.gz) 文件名，避免 HF 根路径/同名冲突
        shutil.copy2(peer_src, staging / "assets" / "arpi_peer_stats.json")

    (staging / "dataset_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    quality_src = output_dir / "quality_report.json"
    if quality_src.exists():
        shutil.copy2(quality_src, staging / "quality_report.json")
    (staging / "README.md").write_text(_hf_readme_text(meta), encoding="utf-8")

    if zip_path is None:
        zip_path = output_dir.parent / "hf_upload.zip"
    zip_path = Path(zip_path)
    if zip_path.exists():
        zip_path.unlink()

    _log(f"writing HF zip -> {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(staging.rglob("*")):
            if not p.is_file():
                continue
            # 双保险：绝不打进 salt / 根级 peer_stats / gz
            rel = p.relative_to(staging).as_posix()
            if rel == ".dataset_salt" or rel.endswith(".gz"):
                continue
            if rel in {"peer_stats.json", "peer_stats.json.gz"}:
                continue
            zf.write(p, arcname=rel)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    bad = [
        n
        for n in names
        if n.endswith(".gz")
        or Path(n).name in {"peer_stats.json", "peer_stats.json.gz", ".dataset_salt"}
    ]
    if bad:
        raise RuntimeError(f"HF zip 含不合规文件: {bad}")
    _log(f"HF zip members={len(names)} size_mb={zip_path.stat().st_size / 1024 / 1024:.2f}")
    return zip_path


def _hf_readme_text(meta: dict) -> str:
    return f"""---
license: other
pretty_name: maimaiDX Desensitized Score Dataset
tags:
  - maimai-dx
  - anonymized
  - rhythm-game
task_categories:
  - other
size_categories:
  - n<1K
configs:
  - config_name: players
    data_files: data/players.jsonl
  - config_name: rating_trends
    data_files: data/rating_trends.jsonl
  - config_name: roast_training_samples
    data_files: data/roast_training_samples.jsonl
---

# maimaiDX 脱敏成绩数据集

- 玩家：{meta.get("player_count")}（已排除不同意共享）
- 同段桶：{meta.get("bucket_count")}
- 生成：{meta.get("generated_at")}

## 文件

| 路径 | 说明 |
|------|------|
| `data/players.jsonl` | 匿名玩家（B50 + 全量成绩 + Rating 趋势） |
| `data/rating_trends.jsonl` | 推分趋势 |
| `data/roast_training_samples.jsonl` | 锐评/提示词轻量样本 |
| `assets/arpi_peer_stats.json` | 同段 ARPI 统计（接入 Bot 时复制为 `peer_stats.json`） |
| `dataset_meta.json` | 导出元信息 |
| `quality_report.json` | 质检与跳过原因 |

## 脱敏

- 无 QQ / 昵称；`player_id` 为单向哈希
- 发送「不同意共享我的数据」的用户已排除

## 接入锐评

```bash
cp assets/arpi_peer_stats.json /path/to/b50_assets/peer_stats.json
```
"""


def _feasibility_hint(payload: dict) -> str:
    delta = payload.get("rating_delta")
    trend = payload.get("rating_trend") or []
    if delta is None or len(trend) < 2:
        return "趋势样本不足，推分建议偏保守"
    days = max(1, len(trend) - 1)
    daily = delta / days
    if daily >= 3:
        return "近期推分较快，可给更具进攻性的路线"
    if daily >= 1:
        return "稳步上升，推分候选以稳定吃分为主"
    if daily >= 0:
        return "几乎横盘，优先修地板与高收益寸止谱"
    return "近期下滑或波动，锐评应强调止损与巩固基本盘"


def _readme_text(meta: dict) -> str:
    return f"""# maimaiDX 脱敏公开数据集

生成时间：{meta.get("generated_at")}
玩家样本：{meta.get("player_count")}（已排除不同意共享）
同段桶数：{meta.get("bucket_count")}（bucket_size={meta.get("bucket_size")}）

## 文件说明

| 文件 | 用途 |
|------|------|
| `peer_stats.json` / `.json.gz` | 复制到 `B50_ASSETS_PATH`，供锐评 ARPI / 同段均值 |
| `players/*.json` | 匿名玩家最新 B50 + 全量成绩 + Rating 趋势（可用 `--no-full-records` 关掉全量） |
| `rating_trends.jsonl` | 推分趋势行式样本 |
| `roast_training_samples.jsonl` | 锐评提示词 / 可行性优化轻量样本 |
| `dataset_meta.json` | 导出元信息 |
| `.dataset_salt` | **私密**匿名盐，勿随公开包发布 |
| `hf_upload/` + 同级 `hf_upload.zip` | Hugging Face 合规上传包（默认自动生成） |

## 脱敏规则

- 去除 QQ、昵称等身份字段
- `player_id` = SHA256 截断，单向不可反查
- 已发送「不同意共享我的数据」的用户不会出现在本数据集

## 接入锐评

```bash
cp peer_stats.json.gz /path/to/b50_assets/
# 或 peer_stats.json / peer_stats.zip
```

重启 Bot 或重新加载 peer_stats 后，`锐评一下` 将使用新同段样本。

## Hugging Face

导出完成后默认生成 `hf_upload.zip`（根目录无 `peer_stats.json` / `.gz` 撞名）。
上传该 zip 即可；可用 `--no-hf-zip` 关闭。

## 重新导出

```bash
python scripts/export_public_dataset.py --output data/public_dataset
```
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="导出脱敏公开成绩数据集")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "public_dataset",
        help="输出目录",
    )
    parser.add_argument(
        "--scores-dir",
        type=Path,
        default=DEFAULT_SCORES_DIR,
        help="user_scores 根目录",
    )
    parser.add_argument(
        "--share-config",
        type=Path,
        default=ROOT / "data" / "data_share_config.json",
        help="数据共享 opt-out 配置",
    )
    parser.add_argument("--bucket-size", type=int, default=200)
    parser.add_argument("--min-bucket-players", type=int, default=8)
    parser.add_argument(
        "--min-chart-samples",
        type=int,
        default=3,
        help="同段谱面最少样本数（不超过该桶玩家数）",
    )
    parser.add_argument("--trend-days", type=int, default=90)
    parser.add_argument(
        "--salt",
        default=None,
        help="匿名哈希盐；默认读 MAIMAIDX_DATASET_SALT 或随机生成",
    )
    parser.add_argument(
        "--include-full-records",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="在 players/*.json 中附带全量成绩（默认开启；可用 --no-full-records 关闭）",
    )
    parser.add_argument(
        "--hf-zip",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="导出后自动打包 Hugging Face 合规 zip（默认开启；--no-hf-zip 关闭）",
    )
    parser.add_argument(
        "--hf-zip-path",
        type=Path,
        default=None,
        help="HF zip 输出路径（默认：<output 同级>/hf_upload.zip）",
    )
    parser.add_argument(
        "--music-data",
        type=Path,
        default=None,
        help="曲库 JSON（含 is_new），用于正确划分 B35/B15",
    )
    parser.add_argument("--min-records", type=int, default=30, help="最少全量成绩条数")
    parser.add_argument("--min-rating", type=int, default=1000, help="最低 rating")
    parser.add_argument("--min-b50", type=int, default=20, help="最少 B50 谱面数")
    parser.add_argument(
        "--min-trend-points",
        type=int,
        default=0,
        help="最少趋势点数（0=不限制）",
    )
    parser.add_argument(
        "--require-trend",
        action="store_true",
        help="必须至少 2 个趋势点才导出该玩家",
    )
    args = parser.parse_args()

    print(f"[export] scores={args.scores_dir}", flush=True)
    print(f"[export] share_config={args.share_config}", flush=True)
    print(f"[export] output={args.output}", flush=True)
    print(f"[export] include_full_records={args.include_full_records}", flush=True)
    print(f"[export] hf_zip={args.hf_zip}", flush=True)
    print(f"[export] music_data={args.music_data}", flush=True)
    t0 = time.time()
    meta = export_dataset(
        scores_dir=args.scores_dir,
        share_config=args.share_config,
        output_dir=args.output,
        bucket_size=args.bucket_size,
        min_bucket_players=args.min_bucket_players,
        min_chart_samples=args.min_chart_samples,
        trend_days=args.trend_days,
        salt=args.salt,
        include_full_records=args.include_full_records,
        pack_hf=args.hf_zip,
        hf_zip_path=args.hf_zip_path,
        music_data=args.music_data,
        min_records=args.min_records,
        min_rating=args.min_rating,
        min_b50=args.min_b50,
        min_trend_points=args.min_trend_points,
        require_trend=args.require_trend,
    )
    elapsed = time.time() - t0
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"[export] done in {elapsed:.1f}s")
    print(f"[export] peer_stats -> {args.output / 'peer_stats.json'}")
    print(f"[export] quality_report -> {args.output / 'quality_report.json'}")
    if meta.get("hf_upload_zip"):
        print(f"[export] hf_upload_zip -> {meta['hf_upload_zip']}")
    print("提醒：公开发布用 hf_upload.zip；勿发布 .dataset_salt。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
