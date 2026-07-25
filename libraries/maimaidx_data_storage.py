"""
用户成绩数据存储模块（高效多快照版）

设计目标：
- 默认只保留同一天最新一份（避免补存/定时任务导致同日堆积）
- 写入只追加一个快照文件 + 更新索引，便于高效查询
- 查询历史优先走索引，不扫描全量文件
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

from loguru import logger as log

# 数据存储路径
DATA_DIR = Path(__file__).parent.parent / "data" / "user_scores"
CONFIG_FILE = Path(__file__).parent.parent / "data" / "storage_config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ScoreRecord:
    song_id: int
    title: str
    level: str
    level_index: int
    ds: float
    achievements: float
    rate: str
    ra: int
    fc: Optional[str] = None
    fs: Optional[str] = None
    dxScore: int = 0


@dataclass
class DailySnapshot:
    date: str  # YYYY-MM-DD
    qqid: int
    nickname: str
    rating: int
    records: List[ScoreRecord]
    record_count: int
    snapshot_id: str = ""  # YYYYMMDD_HHMMSS_xxx
    stored_at: str = ""  # ISO datetime
    source: str = "manual"  # manual / auto /补存


class DataStorageManager:
    def __init__(self):
        self._ensure_config_file()

    def _ensure_config_file(self):
        if not CONFIG_FILE.exists():
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._save_config({"enabled_users": []})

    def _load_config(self) -> Dict[str, Any]:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"[DataStorage] 加载配置失败: {e}")
            return {"enabled_users": []}

    def _save_config(self, config: Dict[str, Any]):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"[DataStorage] 保存配置失败: {e}")

    def is_enabled(self, qqid: int) -> bool:
        return qqid in self._load_config().get("enabled_users", [])

    def enable_user(self, qqid: int) -> bool:
        try:
            config = self._load_config()
            enabled = config.get("enabled_users", [])
            if qqid not in enabled:
                enabled.append(qqid)
                config["enabled_users"] = enabled
                self._save_config(config)
                log.info(f"[DataStorage] 用户 {qqid} 已开启数据存储")
            return True
        except Exception as e:
            log.error(f"[DataStorage] 开启用户 {qqid} 数据存储失败: {e}")
            return False

    def disable_user(self, qqid: int) -> bool:
        try:
            config = self._load_config()
            enabled = config.get("enabled_users", [])
            if qqid in enabled:
                enabled.remove(qqid)
                config["enabled_users"] = enabled
                self._save_config(config)
                log.info(f"[DataStorage] 用户 {qqid} 已关闭数据存储")
            return True
        except Exception as e:
            log.error(f"[DataStorage] 关闭用户 {qqid} 数据存储失败: {e}")
            return False

    def get_enabled_users(self) -> List[int]:
        return self._load_config().get("enabled_users", [])

    def _user_dir(self, qqid: int) -> Path:
        # 只读路径不 mkdir，避免查询历史时留下空目录污染数据集扫描
        return DATA_DIR / str(qqid)

    def _ensure_user_dir(self, qqid: int) -> Path:
        d = self._user_dir(qqid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_file(self, qqid: int) -> Path:
        return self._user_dir(qqid) / "index.json"

    def _snapshot_file(self, qqid: int, snapshot_id: str) -> Path:
        return self._user_dir(qqid) / f"{snapshot_id}.json"

    def _load_index(self, qqid: int) -> Dict[str, Any]:
        path = self._index_file(qqid)
        if not path.exists():
            return {"qqid": qqid, "snapshots": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "snapshots" not in data:
                data["snapshots"] = []
            return data
        except Exception as e:
            log.error(f"[DataStorage] 加载用户 {qqid} 索引失败: {e}")
            return {"qqid": qqid, "snapshots": []}

    def _save_index(self, qqid: int, index_data: Dict[str, Any]) -> bool:
        try:
            self._ensure_user_dir(qqid)
            with open(self._index_file(qqid), "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            log.error(f"[DataStorage] 保存用户 {qqid} 索引失败: {e}")
            return False

    def _build_snapshot_id(self) -> str:
        now = datetime.now()
        return now.strftime("%Y%m%d_%H%M%S_%f")

    def _dedupe_keep_latest_per_day(self, qqid: int, index_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        同一天只保留最新一份快照（按 stored_at 倒序后的第一条）。
        会删除被淘汰的快照文件，并回写索引。
        """
        snaps: List[Dict[str, Any]] = list(index_data.get("snapshots", []) or [])
        if len(snaps) <= 1:
            return index_data

        # 先保证新到旧
        try:
            snaps.sort(key=lambda x: x.get("stored_at", ""), reverse=True)
        except Exception:
            pass

        keep: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        seen_dates = set()
        for m in snaps:
            d = m.get("date")
            if not d:
                # 无 date 的脏数据：保留（但尽量放在后面）
                keep.append(m)
                continue
            if d in seen_dates:
                removed.append(m)
            else:
                seen_dates.add(d)
                keep.append(m)

        if not removed:
            index_data["snapshots"] = keep
            return index_data

        # 删除多余快照文件（尽力而为）
        removed_ids = []
        for m in removed:
            sid = str(m.get("snapshot_id") or "")
            if not sid:
                continue
            removed_ids.append(sid)
            try:
                p = self._snapshot_file(qqid, sid)
                if p.exists():
                    p.unlink()
            except Exception as e:
                log.warning(f"[DataStorage] 删除旧快照失败 user={qqid} id={sid}: {e}")

        index_data["snapshots"] = keep
        # last_stored_at 以当前最新快照为准
        if keep:
            index_data["last_stored_at"] = keep[0].get("stored_at", index_data.get("last_stored_at"))

        log.info(
            f"[DataStorage] 索引去重完成 user={qqid} removed={len(removed_ids)} kept={len(keep)} "
            f"(同日仅保留最新)"
        )
        return index_data

    def save_daily_snapshot(self, snapshot: DailySnapshot) -> bool:
        try:
            now = datetime.now()
            if not snapshot.stored_at:
                snapshot.stored_at = now.isoformat(timespec="seconds")
            if not snapshot.snapshot_id:
                snapshot.snapshot_id = self._build_snapshot_id()

            payload = {
                "snapshot_id": snapshot.snapshot_id,
                "stored_at": snapshot.stored_at,
                "source": snapshot.source,
                "date": snapshot.date,
                "qqid": snapshot.qqid,
                "nickname": snapshot.nickname,
                "rating": snapshot.rating,
                "record_count": snapshot.record_count,
                "records": [
                    {
                        "song_id": r.song_id,
                        "title": r.title,
                        "level": r.level,
                        "level_index": r.level_index,
                        "ds": r.ds,
                        "achievements": r.achievements,
                        "rate": r.rate,
                        "ra": r.ra,
                        "fc": r.fc,
                        "fs": r.fs,
                        "dxScore": r.dxScore,
                    }
                    for r in snapshot.records
                ],
            }
            self._ensure_user_dir(snapshot.qqid)
            with open(self._snapshot_file(snapshot.qqid, snapshot.snapshot_id), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            index_data = self._load_index(snapshot.qqid)
            index_data["qqid"] = snapshot.qqid
            index_data["nickname"] = snapshot.nickname
            index_data["last_stored_at"] = snapshot.stored_at
            index_data["snapshots"].append(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "date": snapshot.date,
                    "stored_at": snapshot.stored_at,
                    "source": snapshot.source,
                    "rating": snapshot.rating,
                    "record_count": snapshot.record_count,
                }
            )
            # 新到旧
            index_data["snapshots"].sort(key=lambda x: x["stored_at"], reverse=True)
            # 同一天只保留最新一份，并清理旧文件
            index_data = self._dedupe_keep_latest_per_day(snapshot.qqid, index_data)
            if not self._save_index(snapshot.qqid, index_data):
                return False

            log.info(
                f"[DataStorage] 已保存快照 user={snapshot.qqid} id={snapshot.snapshot_id} "
                f"date={snapshot.date} records={snapshot.record_count} rating={snapshot.rating}"
            )
            return True
        except Exception as e:
            log.error(f"[DataStorage] 保存用户 {snapshot.qqid} 成绩快照失败: {e}")
            return False

    def load_snapshot_by_id(self, qqid: int, snapshot_id: str) -> Optional[DailySnapshot]:
        try:
            path = self._snapshot_file(qqid, snapshot_id)
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = [
                ScoreRecord(
                    song_id=r["song_id"],
                    title=r["title"],
                    level=r["level"],
                    level_index=r["level_index"],
                    ds=r["ds"],
                    achievements=r["achievements"],
                    rate=r["rate"],
                    ra=r["ra"],
                    fc=r.get("fc"),
                    fs=r.get("fs"),
                    dxScore=r.get("dxScore", 0),
                )
                for r in data.get("records", [])
            ]
            return DailySnapshot(
                date=data["date"],
                qqid=data["qqid"],
                nickname=data["nickname"],
                rating=data["rating"],
                records=records,
                record_count=data.get("record_count", len(records)),
                snapshot_id=data.get("snapshot_id", snapshot_id),
                stored_at=data.get("stored_at", ""),
                source=data.get("source", "manual"),
            )
        except Exception as e:
            log.error(f"[DataStorage] 加载用户 {qqid} 快照 {snapshot_id} 失败: {e}")
            return None

    def load_daily_snapshot(self, qqid: int, date: str) -> Optional[DailySnapshot]:
        """
        兼容旧接口：按日期加载“最新一次”快照。
        """
        try:
            index_data = self._load_index(qqid)
            for meta in index_data.get("snapshots", []):
                if meta.get("date") == date:
                    return self.load_snapshot_by_id(qqid, meta["snapshot_id"])

            # 兼容旧文件命名：YYYY-MM-DD.json
            old_file = self._user_dir(qqid) / f"{date}.json"
            if old_file.exists():
                with open(old_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                records = [
                    ScoreRecord(
                        song_id=r["song_id"],
                        title=r["title"],
                        level=r["level"],
                        level_index=r["level_index"],
                        ds=r["ds"],
                        achievements=r["achievements"],
                        rate=r["rate"],
                        ra=r["ra"],
                        fc=r.get("fc"),
                        fs=r.get("fs"),
                        dxScore=r.get("dxScore", 0),
                    )
                    for r in data.get("records", [])
                ]
                return DailySnapshot(
                    date=data["date"],
                    qqid=data["qqid"],
                    nickname=data["nickname"],
                    rating=data["rating"],
                    records=records,
                    record_count=data.get("record_count", len(records)),
                    snapshot_id=f"legacy_{date}",
                    stored_at="",
                    source="legacy",
                )
            return None
        except Exception as e:
            log.error(f"[DataStorage] 加载用户 {qqid} {date} 成绩快照失败: {e}")
            return None

    def list_snapshots(self, qqid: int, limit: int = 30) -> List[Dict[str, Any]]:
        index_data = self._load_index(qqid)
        return index_data.get("snapshots", [])[:limit]

    def get_user_history(self, qqid: int, days: int = 30) -> List[DailySnapshot]:
        snapshots: List[DailySnapshot] = []
        metas = self.list_snapshots(qqid, limit=days)
        for meta in metas:
            snap = self.load_snapshot_by_id(qqid, meta["snapshot_id"])
            if snap:
                snapshots.append(snap)
        return snapshots

    def get_rating_history(self, qqid: int, days: int = 30) -> List[Dict[str, Any]]:
        metas = self.list_snapshots(qqid, limit=days)
        return [
            {
                "snapshot_id": m.get("snapshot_id"),
                "date": m.get("date"),
                "stored_at": m.get("stored_at"),
                "source": m.get("source"),
                "rating": m.get("rating", 0),
                "record_count": m.get("record_count", 0),
            }
            for m in metas
        ]

    def list_snapshots_in_period_chronological(self, qqid: int, days: int) -> List[DailySnapshot]:
        """时间窗口内的完整快照列表，按 stored_at 升序（无 stored_at 的 legacy 快照按 date 排在末尾）。"""
        index_data = self._load_index(qqid)
        metas = index_data.get("snapshots", [])
        if not metas:
            return []
        now = datetime.now()
        cutoff = now - timedelta(days=days)
        selected: List[DailySnapshot] = []
        for m in metas:
            stored_at = m.get("stored_at") or ""
            if not stored_at:
                continue
            try:
                dt = datetime.fromisoformat(stored_at)
            except Exception:
                continue
            if dt >= cutoff:
                snap = self.load_snapshot_by_id(qqid, m.get("snapshot_id", ""))
                if snap:
                    selected.append(snap)
        selected.sort(key=lambda s: s.stored_at or f"{s.date}T00:00:00")
        return selected

    def rating_delta_in_period(self, qqid: int, days: int) -> Optional[Tuple[int, int, int]]:
        """
        窗口内最早一次与最近一次存档的 rating 差。
        Returns: (old_rating, new_rating, delta)，快照不足 2 次则 None。
        """
        snaps = self.list_snapshots_in_period_chronological(qqid, days)
        if len(snaps) < 2:
            return None
        old_r = int(snaps[0].rating)
        new_r = int(snaps[-1].rating)
        return (old_r, new_r, new_r - old_r)


data_storage = DataStorageManager()
