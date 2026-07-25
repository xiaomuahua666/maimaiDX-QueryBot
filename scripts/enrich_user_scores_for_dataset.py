#!/usr/bin/env python3
"""扩充 user_scores：从 player_cache / 备份快照灌入，供公开数据集导出。

来源（均尊重 data_share opt-out）：
1. 当前 player_cache.db 中带全量成绩的缓存（忽略 TTL）
2. 备份目录里的 player_cache.db / user_scores
3. 可选：对仍无快照的 enabled_users 调查分器补存（需 Bot 环境）

用法：
  python scripts/enrich_user_scores_for_dataset.py
  python scripts/enrich_user_scores_for_dataset.py --api-backfill --concurrency 3
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]


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
    DailySnapshot,
    DataStorageManager,
    ScoreRecord,
)


def _log(msg: str) -> None:
    print(f"[enrich] {msg}", flush=True)


def _has_usable_snapshot(scores_dir: Path, qqid: str) -> bool:
    idx = scores_dir / qqid / "index.json"
    if not idx.exists():
        return False
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except Exception:
        return False
    snaps = data.get("snapshots") or []
    if not snaps:
        return False
    sid = str(snaps[0].get("snapshot_id") or "")
    return bool(sid) and (scores_dir / qqid / f"{sid}.json").exists()


def _record_from_dict(r: dict) -> Optional[ScoreRecord]:
    try:
        song_id = int(r.get("song_id") or r.get("music_id") or 0)
        level_index = int(r.get("level_index"))
        ach = float(r.get("achievements") if r.get("achievements") is not None else r.get("achievement"))
        ds = float(r.get("ds") or 0)
        ra = int(r.get("ra") or r.get("rating") or 0)
    except (TypeError, ValueError):
        return None
    if song_id <= 0 or not (0.0 <= ach <= 101.0):
        return None
    return ScoreRecord(
        song_id=song_id,
        title=str(r.get("title") or "")[:200],
        level=str(r.get("level") or "")[:16],
        level_index=level_index,
        ds=ds,
        achievements=round(ach, 4),
        rate=str(r.get("rate") or "")[:16],
        ra=ra,
        fc=(str(r["fc"]) if r.get("fc") else None),
        fs=(str(r["fs"]) if r.get("fs") else None),
        dxScore=max(0, int(r.get("dxScore") or r.get("dx_score") or 0)),
    )


def _iter_cache_dbs(paths: Iterable[Path]) -> List[Path]:
    out: List[Path] = []
    seen: Set[str] = set()
    for p in paths:
        if not p or not Path(p).exists():
            continue
        rp = str(Path(p).resolve())
        if rp in seen:
            continue
        seen.add(rp)
        out.append(Path(p))
    return out


def _best_cache_rows(db_path: Path) -> Dict[int, dict]:
    """qqid -> best row (最多 records，其次最新 fetched_at)。"""
    best: Dict[int, dict] = {}
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT qqid, username, source, userinfo_json, records_json, fetched_at
            FROM player_cache
            WHERE qqid IS NOT NULL
              AND records_json IS NOT NULL
              AND length(records_json) > 20
            """
        ).fetchall()
        con.close()
    except Exception as e:
        _log(f"read cache failed {db_path}: {e}")
        return {}

    for row in rows:
        try:
            qqid = int(row["qqid"])
            records = json.loads(row["records_json"])
            if not isinstance(records, list) or len(records) < 10:
                continue
            userinfo = json.loads(row["userinfo_json"] or "{}")
            fetched_at = float(row["fetched_at"] or 0)
        except Exception:
            continue
        cur = best.get(qqid)
        score = (len(records), fetched_at)
        if cur is None or score > (cur["_n"], cur["_fetched_at"]):
            best[qqid] = {
                "qqid": qqid,
                "records": records,
                "userinfo": userinfo if isinstance(userinfo, dict) else {},
                "source": str(row["source"] or "cache"),
                "_n": len(records),
                "_fetched_at": fetched_at,
            }
    return best


def ingest_from_caches(
    *,
    scores_dir: Path,
    cache_dbs: List[Path],
    opted_out: Set[str],
    min_records: int = 30,
) -> dict:
    from nonebot_plugin_maimaidx.libraries import maimaidx_data_storage as _ds_mod

    storage = DataStorageManager()
    original = _ds_mod.DATA_DIR
    _ds_mod.DATA_DIR = scores_dir
    written = 0
    skipped_has = 0
    skipped_opt = 0
    skipped_thin = 0
    merged: Dict[int, dict] = {}
    try:
        for db in cache_dbs:
            _log(f"scan cache {db}")
            part = _best_cache_rows(db)
            for qqid, row in part.items():
                prev = merged.get(qqid)
                if prev is None or (row["_n"], row["_fetched_at"]) > (prev["_n"], prev["_fetched_at"]):
                    merged[qqid] = row
        _log(f"cache candidates={len(merged)}")

        total = len(merged)
        for idx, (qqid, row) in enumerate(merged.items(), 1):
            qqid_s = str(qqid)
            if qqid_s in opted_out:
                skipped_opt += 1
                continue
            if _has_usable_snapshot(scores_dir, qqid_s):
                skipped_has += 1
                continue
            records: List[ScoreRecord] = []
            bad_rows = 0
            for r in row["records"]:
                if isinstance(r, dict):
                    sr = _record_from_dict(r)
                    if sr:
                        records.append(sr)
                    else:
                        bad_rows += 1
            if len(records) < min_records:
                skipped_thin += 1
                if skipped_thin <= 10:
                    _log(
                        f"cache thin skip qq={qqid_s} valid={len(records)} "
                        f"raw={row['_n']} bad={bad_rows} min={min_records}"
                    )
                continue
            ui = row["userinfo"]
            rating = int(ui.get("rating") or 0)
            if rating <= 0:
                rating = max((x.ra for x in records), default=0)
            fetched_at = float(row["_fetched_at"] or time.time())
            try:
                dt = datetime.fromtimestamp(fetched_at)
            except Exception:
                dt = datetime.now()
            snap = DailySnapshot(
                date=dt.strftime("%Y-%m-%d"),
                qqid=qqid,
                nickname=str(ui.get("nickname") or ui.get("username") or qqid_s),
                rating=rating,
                records=records,
                record_count=len(records),
                stored_at=dt.isoformat(timespec="seconds"),
                source=f"cache:{row.get('source') or 'unknown'}",
            )
            try:
                if storage.save_daily_snapshot(snap):
                    written += 1
                    if written <= 20 or written % 50 == 0:
                        _log(
                            f"cache written qq={qqid_s} rating={rating} "
                            f"records={len(records)} total_written={written}"
                        )
                else:
                    _log(f"cache save returned false qq={qqid_s}")
            except Exception as e:
                _log(f"cache save fail qq={qqid_s}: {type(e).__name__}: {e}")
            if idx % 200 == 0:
                _log(
                    f"cache progress {idx}/{total} written={written} "
                    f"skip_has={skipped_has} opt={skipped_opt} thin={skipped_thin}"
                )
    finally:
        _ds_mod.DATA_DIR = original

    summary = {
        "written": written,
        "candidates": len(merged),
        "skipped_has_snapshot": skipped_has,
        "skipped_opt_out": skipped_opt,
        "skipped_thin": skipped_thin,
        "cache_dbs": [str(p) for p in cache_dbs],
    }
    _log(f"cache ingest done: {summary}")
    return summary


def _safe_replace_user_dir(src: Path, dest: Path) -> None:
    """目标已存在（含空目录）时先删再复制，避免 copytree FileExistsError。"""
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def ingest_from_backup_scores(
    *,
    scores_dir: Path,
    backup_scores_dirs: List[Path],
    opted_out: Set[str],
) -> dict:
    """把备份里有 index 而当前没有可用快照的用户目录复制过来。"""
    copied = 0
    skipped_has = 0
    skipped_opt = 0
    skipped_no_index = 0
    skipped_copy_fail = 0
    replaced_empty_or_broken = 0
    scanned_users = 0

    for backup in backup_scores_dirs:
        if not backup.exists():
            _log(f"backup scores missing, skip: {backup}")
            continue
        user_dirs = sorted(p for p in backup.iterdir() if p.is_dir() and p.name.isdigit())
        _log(f"scan backup scores {backup} users={len(user_dirs)}")
        for user_dir in user_dirs:
            scanned_users += 1
            qqid_s = user_dir.name
            if qqid_s in opted_out:
                skipped_opt += 1
                continue
            if _has_usable_snapshot(scores_dir, qqid_s):
                skipped_has += 1
                continue
            if not (user_dir / "index.json").exists():
                skipped_no_index += 1
                continue

            dest = scores_dir / qqid_s
            dest_existed = dest.exists()
            try:
                if dest_existed:
                    replaced_empty_or_broken += 1
                    _log(
                        f"backup replace broken/empty dest qq={qqid_s} "
                        f"from={user_dir}"
                    )
                _safe_replace_user_dir(user_dir, dest)
            except Exception as e:
                skipped_copy_fail += 1
                _log(f"backup copy fail qq={qqid_s}: {type(e).__name__}: {e}")
                continue

            if _has_usable_snapshot(scores_dir, qqid_s):
                copied += 1
                if copied <= 20 or copied % 50 == 0:
                    _log(f"backup copied ok qq={qqid_s} total={copied}")
            else:
                skipped_copy_fail += 1
                _log(f"backup copied but still unusable, rollback qq={qqid_s}")
                shutil.rmtree(dest, ignore_errors=True)

            if scanned_users % 200 == 0:
                _log(
                    f"backup progress scanned={scanned_users} copied={copied} "
                    f"skip_has={skipped_has} skip_opt={skipped_opt} "
                    f"no_index={skipped_no_index} fail={skipped_copy_fail}"
                )

    summary = {
        "copied": copied,
        "scanned_users": scanned_users,
        "skipped_has_snapshot": skipped_has,
        "skipped_opt_out": skipped_opt,
        "skipped_no_index": skipped_no_index,
        "skipped_copy_fail": skipped_copy_fail,
        "replaced_empty_or_broken": replaced_empty_or_broken,
        "backup_dirs": [str(p) for p in backup_scores_dirs],
    }
    _log(f"backup ingest done: {summary}")
    return summary


async def api_backfill_enabled(
    *,
    scores_dir: Path,
    opted_out: Set[str],
    concurrency: int = 3,
    limit: int = 0,
) -> dict:
    """对开启存储但仍无可用快照的用户拉查分器补存。"""
    import asyncio

    from nonebot_plugin_maimaidx.libraries import maimaidx_data_storage as _ds_mod
    from nonebot_plugin_maimaidx.libraries.maimaidx_data_storage import data_storage

    # 避免 import scheduler 注册 cron：直接复用其核心逻辑的薄包装
    from nonebot_plugin_maimaidx.libraries.maimaidx_datasource import get_user_records

    original = _ds_mod.DATA_DIR
    _ds_mod.DATA_DIR = scores_dir
    try:
        enabled = [int(x) for x in data_storage.get_enabled_users()]
        targets = []
        for qq in enabled:
            if str(qq) in opted_out:
                continue
            if _has_usable_snapshot(scores_dir, str(qq)):
                continue
            targets.append(qq)
        if limit > 0:
            targets = targets[:limit]
        _log(f"api backfill targets={len(targets)} concurrency={concurrency}")

        sem = asyncio.Semaphore(max(1, concurrency))
        ok = 0
        fail = 0

        async def one(qqid: int) -> bool:
            nonlocal ok, fail
            async with sem:
                try:
                    userinfo, dev_records = await get_user_records(
                        qqid=qqid, force_refresh=True
                    )
                    records = []
                    for r in dev_records or []:
                        records.append(
                            ScoreRecord(
                                song_id=r.song_id,
                                title=r.title,
                                level=r.level,
                                level_index=r.level_index,
                                ds=r.ds,
                                achievements=r.achievements,
                                rate=r.rate,
                                ra=r.ra,
                                fc=r.fc,
                                fs=r.fs,
                                dxScore=getattr(r, "dxScore", 0) or 0,
                            )
                        )
                    if len(records) < 10:
                        fail += 1
                        return False
                    snap = DailySnapshot(
                        date=datetime.now().strftime("%Y-%m-%d"),
                        qqid=qqid,
                        nickname=str(
                            getattr(userinfo, "nickname", None)
                            or getattr(userinfo, "username", None)
                            or qqid
                        ),
                        rating=int(getattr(userinfo, "rating", 0) or 0),
                        records=records,
                        record_count=len(records),
                        source="dataset_backfill",
                    )
                    storage = DataStorageManager()
                    good = storage.save_daily_snapshot(snap)
                    if good:
                        ok += 1
                    else:
                        fail += 1
                    return good
                except Exception as e:
                    fail += 1
                    _log(f"api backfill fail qq={qqid}: {type(e).__name__}: {e}")
                    return False

        await asyncio.gather(*(one(q) for q in targets))
        return {"targets": len(targets), "ok": ok, "fail": fail}
    finally:
        _ds_mod.DATA_DIR = original


def _discover_paths(plugin_dir: Path) -> Tuple[List[Path], List[Path]]:
    caches: List[Path] = []
    backups: List[Path] = []
    live_cache = plugin_dir / "data" / "player_cache" / "player_cache.db"
    if live_cache.exists():
        caches.append(live_cache)
    # 常见服务器备份位置
    for base in (
        Path("/www/bot/backups"),
        plugin_dir.parent,
        Path("/www/bot"),
    ):
        if not base.exists():
            continue
        for p in base.rglob("player_cache.db"):
            caches.append(p)
        for p in base.rglob("user_scores"):
            if p.is_dir() and p.resolve() != (plugin_dir / "data" / "user_scores").resolve():
                backups.append(p)
    # 去重保序
    caches = _iter_cache_dbs(caches)
    uniq_b: List[Path] = []
    seen: Set[str] = set()
    for b in backups:
        rp = str(b.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        uniq_b.append(b)
    return caches, uniq_b


def main() -> int:
    parser = argparse.ArgumentParser(description="从缓存/备份扩充 user_scores")
    parser.add_argument(
        "--scores-dir",
        type=Path,
        default=DEFAULT_SCORES_DIR,
    )
    parser.add_argument(
        "--share-config",
        type=Path,
        default=ROOT / "data" / "data_share_config.json",
    )
    parser.add_argument(
        "--cache-db",
        action="append",
        default=[],
        help="额外 player_cache.db，可重复",
    )
    parser.add_argument(
        "--backup-scores",
        action="append",
        default=[],
        help="额外备份 user_scores 目录，可重复",
    )
    parser.add_argument("--min-records", type=int, default=30)
    parser.add_argument(
        "--api-backfill",
        action="store_true",
        help="对开启存储但仍无快照的用户调用查分器补存（较慢）",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--api-limit", type=int, default=0, help="API 补存最多人数，0=不限制")
    parser.add_argument(
        "--no-auto-discover",
        action="store_true",
        help="不自动扫描 /www/bot 备份",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="enrich 报告输出路径（默认 data/enrich_report_<时间戳>.json）",
    )
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    scores_dir = Path(args.scores_dir)
    scores_dir.mkdir(parents=True, exist_ok=True)
    opted_out = set(DataShareManager(Path(args.share_config)).list_opted_out())
    _log(f"stamp={stamp}")
    _log(f"scores_dir={scores_dir}")
    _log(f"share_config={args.share_config}")
    _log(f"opted_out={len(opted_out)} ids_sample={sorted(opted_out)[:10]}")
    _log(f"min_records={args.min_records} api_backfill={args.api_backfill}")

    cache_dbs = [Path(p) for p in args.cache_db]
    backup_dirs = [Path(p) for p in args.backup_scores]
    if not args.no_auto_discover:
        auto_c, auto_b = _discover_paths(ROOT)
        cache_dbs = _iter_cache_dbs(cache_dbs + auto_c)
        # backup scores: keep unique
        seen = {str(p.resolve()) for p in backup_dirs if p.exists()}
        for b in auto_b:
            rp = str(b.resolve())
            if rp not in seen:
                backup_dirs.append(b)
                seen.add(rp)

    before = sum(
        1
        for d in scores_dir.iterdir()
        if d.is_dir() and d.name.isdigit() and _has_usable_snapshot(scores_dir, d.name)
    )
    _log(f"usable snapshots before={before}")

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "before": before,
    }
    report["backup_scores"] = ingest_from_backup_scores(
        scores_dir=scores_dir,
        backup_scores_dirs=backup_dirs,
        opted_out=opted_out,
    )
    report["cache"] = ingest_from_caches(
        scores_dir=scores_dir,
        cache_dbs=cache_dbs,
        opted_out=opted_out,
        min_records=args.min_records,
    )

    if args.api_backfill:
        import asyncio

        report["api_backfill"] = asyncio.run(
            api_backfill_enabled(
                scores_dir=scores_dir,
                opted_out=opted_out,
                concurrency=args.concurrency,
                limit=args.api_limit,
            )
        )

    after = sum(
        1
        for d in scores_dir.iterdir()
        if d.is_dir() and d.name.isdigit() and _has_usable_snapshot(scores_dir, d.name)
    )
    report["after"] = after
    report["gained"] = after - before
    report["stamp"] = stamp
    out = args.report or (scores_dir.parent / f"enrich_report_{stamp}.json")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同步一份 latest 方便查看
    latest = out.parent / "enrich_report_latest.json"
    try:
        latest.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    _log("===== enrich summary =====")
    _log(json.dumps(report, ensure_ascii=False, indent=2))
    _log(f"usable snapshots: {before} -> {after} (gained {after - before})")
    _log(f"report={out}")
    _log(f"report_latest={latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
