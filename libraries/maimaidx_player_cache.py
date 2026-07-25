"""
玩家成绩本地缓存：减少对水鱼/落雪查分器的重复请求。

- 任意经 datasource 成功拉取的数据会写入 SQLite 缓存
- 未过期时优先读缓存；可选复用「数据存储」最近快照（更长 TTL）
- username 查询单独缓存；落雪与水鱼分 source 存储
"""

from __future__ import annotations

import contextvars
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import List, Optional, Tuple

from ..config import log, maiconfig
from .maimaidx_data_storage import DailySnapshot, ScoreRecord, data_storage
from .maimaidx_model import PlayInfoDev, UserInfo
from .maimaidx_sqlite import configure_sqlite_connection

DB_DIR = Path(__file__).parent.parent / "data" / "player_cache"
DB_FILE = DB_DIR / "player_cache.db"


@dataclass
class CachedPlayerBundle:
    userinfo: UserInfo
    records: List[PlayInfoDev]
    fetched_at: float
    from_storage: bool = False


@dataclass
class PlayerFetchMeta:
    """本次指令取数来源，供成绩图 footer 展示。"""

    fetched_at: float
    origin: str  # api | sqlite_cache | storage_snapshot
    force_refresh: bool = False


_FETCH_META: contextvars.ContextVar[Optional[PlayerFetchMeta]] = contextvars.ContextVar(
    "player_fetch_meta", default=None
)

REFRESH_B50_HINT = '💡 刚打完机台？发送「刷新b50」可从查分器获取最新成绩'


def footer_join_sections(sections: List[List[str]]) -> str:
    """多段 footer 文案，段与段之间空一行。"""
    parts = ['\n'.join(block) for block in sections if block]
    return '\n\n' + '\n\n'.join(parts)


def _set_fetch_meta(
    fetched_at: float,
    origin: str,
    *,
    force_refresh: bool = False,
) -> None:
    _FETCH_META.set(
        PlayerFetchMeta(
            fetched_at=fetched_at,
            origin=origin,
            force_refresh=force_refresh,
        )
    )


def clear_fetch_meta() -> None:
    _FETCH_META.set(None)


def peek_fetch_meta() -> Optional[PlayerFetchMeta]:
    """读取本次请求的取数元信息，不清除。"""
    return _FETCH_META.get()


def will_fetch_from_api(
    qqid: Optional[int],
    username: Optional[str],
    source: str,
    *,
    force_refresh: bool = False,
    need_charts: bool = True,
) -> bool:
    """预判是否会发起查分器 API（用于 BREAK 预检，不写入 fetch meta）。"""
    if force_refresh:
        return True
    ttl = _cache_ttl_seconds()
    hit = player_cache_db.get(qqid, username, source, ttl)
    if hit is None:
        if qqid and not username:
            snap = _try_storage_snapshot(qqid, _storage_fallback_ttl_seconds())
            if snap is not None:
                if need_charts and snap.userinfo.charts is None:
                    return True
                return False
        return True
    if need_charts and hit.userinfo.charts is None:
        return True
    return False


def pop_data_freshness_footer_lines() -> List[str]:
    """取出并清除本次请求的取数元信息，转为 footer 行。"""
    meta = _FETCH_META.get()
    _FETCH_META.set(None)
    if meta is None or _cache_ttl_seconds() <= 0:
        return []
    return _format_freshness_lines(meta)


def _format_freshness_lines(meta: PlayerFetchMeta) -> List[str]:
    ts = datetime.fromtimestamp(meta.fetched_at)
    clock = ts.strftime("%m-%d %H:%M")
    age_s = max(0, int(time.time() - meta.fetched_at))

    if meta.origin == "api":
        if meta.force_refresh:
            label = f"🕐 数据更新：{clock}（刚刚已从查分器同步）"
        else:
            label = f"🕐 数据更新：{clock}（查分器实时）"
        return [label]

    if meta.origin == "storage_snapshot":
        if age_s < 3600:
            age_hint = f"{max(1, age_s // 60)} 分钟前"
        elif age_s < 86400:
            age_hint = f"{age_s // 3600} 小时前"
        else:
            age_hint = f"{age_s // 86400} 天前"
        return [
            f"🕐 数据更新：{clock}（本地存档，约 {age_hint}）",
            REFRESH_B50_HINT,
        ]

    # sqlite_cache
    if age_s < 60:
        age_hint = "刚刚"
    elif age_s < 3600:
        age_hint = f"约 {max(1, age_s // 60)} 分钟前"
    elif age_s < 86400:
        age_hint = f"约 {age_s // 3600} 小时前"
    else:
        age_hint = f"约 {age_s // 86400} 天前"
    return [
        f"🕐 数据更新：{clock}（本地缓存，{age_hint}）",
        REFRESH_B50_HINT,
    ]


def _cache_ttl_seconds() -> int:
    return int(getattr(maiconfig, "maimaidx_player_cache_seconds", 900) or 900)


def _storage_fallback_ttl_seconds() -> int:
    """数据存储快照作为兜底时的最大年龄（默认 24h）。"""
    return int(getattr(maiconfig, "maimaidx_player_storage_fallback_seconds", 86400) or 86400)


def friend_battle_cache_seconds() -> int:
    """友人对战允许使用的本地成绩最大年龄（默认 7 天）。"""
    return int(getattr(maiconfig, "maimaidx_friend_battle_cache_seconds", 604800) or 604800)


def get_cached_b50_for_friend_battle(qqid: int) -> Optional[UserInfo]:
    """友人对战：仅读本地 B50（SQLite / 数据存储），不发起网络请求。"""
    bundle = get_cached_player_for_friend_battle(qqid)
    if bundle is None or bundle.userinfo.charts is None:
        return None
    return bundle.userinfo


def get_cached_rating_for_friend_battle(qqid: int) -> Optional[int]:
    """友人对战：从本地库读取总 rating，无则 None。"""
    bundle = get_cached_player_for_friend_battle(qqid)
    if bundle is None:
        return None
    return int(bundle.userinfo.rating or 0)


def get_cached_player_for_friend_battle(qqid: int) -> Optional[CachedPlayerBundle]:
    """
    友人对战专用：读取 SQLite 玩家缓存或数据存储快照（较长 TTL，重启后仍有效）。
    不触发网络请求。
    """
    ttl = friend_battle_cache_seconds()
    if ttl <= 0:
        return None
    try:
        from .maimaidx_datasource import get_user_source

        source = get_user_source(qqid)
    except Exception:
        source = "divingfish"
    hit = player_cache_db.get(qqid, None, source, ttl)
    if hit is not None and hit.records:
        log.debug(
            f"[PlayerCache] 友人对战命中 SQLite qq={qqid} age={int(time.time() - hit.fetched_at)}s"
        )
        return hit
    snap_hit = _try_storage_snapshot(qqid, ttl)
    if snap_hit is not None:
        log.debug(f"[PlayerCache] 友人对战命中数据存储快照 qq={qqid}")
    return snap_hit


def _use_storage_fallback() -> bool:
    return bool(getattr(maiconfig, "maimaidx_player_cache_use_storage", True))


def _cache_key(qqid: Optional[int], username: Optional[str], source: str) -> str:
    if username:
        return f"u:{source}:{username.strip().lower()}"
    return f"q:{source}:{int(qqid)}"


class PlayerCacheDB:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        configure_sqlite_connection(self._conn)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS player_cache (
                cache_key TEXT PRIMARY KEY,
                qqid INTEGER,
                username TEXT,
                source TEXT NOT NULL,
                userinfo_json TEXT NOT NULL,
                records_json TEXT NOT NULL,
                fetched_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_player_cache_fetched ON player_cache(fetched_at);
        """)
        self._conn.commit()

    def get(
        self, qqid: Optional[int], username: Optional[str], source: str, ttl: int
    ) -> Optional[CachedPlayerBundle]:
        if ttl <= 0:
            return None
        key = _cache_key(qqid, username, source)
        row = self._conn.execute(
            "SELECT userinfo_json, records_json, fetched_at FROM player_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        fetched_at = float(row["fetched_at"])
        if time.time() - fetched_at > ttl:
            return None
        try:
            userinfo = UserInfo.model_validate(json.loads(row["userinfo_json"]))
            records = [
                PlayInfoDev.model_validate(r) for r in json.loads(row["records_json"])
            ]
            return CachedPlayerBundle(
                userinfo=userinfo, records=records, fetched_at=fetched_at
            )
        except Exception as e:
            log.warning(f"[PlayerCache] 解析缓存失败 key={key}: {e}")
            return None

    def set(
        self,
        qqid: Optional[int],
        username: Optional[str],
        source: str,
        userinfo: UserInfo,
        records: List[PlayInfoDev],
    ) -> None:
        key = _cache_key(qqid, username, source)
        now = time.time()
        try:
            ui_json = userinfo.model_dump_json()
            rec_json = json.dumps(
                [r.model_dump(mode="json") for r in records],
                ensure_ascii=False,
            )
        except Exception as e:
            log.warning(f"[PlayerCache] 序列化失败 key={key}: {e}")
            return
        self._conn.execute(
            """
            INSERT OR REPLACE INTO player_cache
            (cache_key, qqid, username, source, userinfo_json, records_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                int(qqid) if qqid else None,
                (username or "").strip() or None,
                source,
                ui_json,
                rec_json,
                now,
            ),
        )
        self._conn.commit()
        log.debug(
            f"[PlayerCache] 已写入 key={key} records={len(records)} rating={userinfo.rating}"
        )

    def delete_by_qqid(self, qqid: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM player_cache WHERE qqid = ?", (int(qqid),)
        )
        self._conn.commit()
        return cur.rowcount

    def list_recent_full_records(
        self,
        *,
        since_ts: float,
        min_records: int = 30,
        limit: int = 2000,
    ) -> List[dict]:
        """忽略 TTL，返回近期带厚全量成绩的缓存（供共享快照日任务）。"""
        rows = self._conn.execute(
            """
            SELECT qqid, userinfo_json, records_json, fetched_at, source
            FROM player_cache
            WHERE qqid IS NOT NULL
              AND fetched_at >= ?
              AND records_json IS NOT NULL
              AND length(records_json) > 20
            ORDER BY fetched_at DESC
            LIMIT ?
            """,
            (float(since_ts), int(limit) * 3),
        ).fetchall()
        best: dict[int, dict] = {}
        for row in rows:
            try:
                qqid = int(row["qqid"])
                raw_records = json.loads(row["records_json"])
                if not isinstance(raw_records, list) or len(raw_records) < min_records:
                    continue
                userinfo = UserInfo.model_validate(json.loads(row["userinfo_json"]))
                records = [PlayInfoDev.model_validate(r) for r in raw_records]
                fetched_at = float(row["fetched_at"] or 0)
            except Exception:
                continue
            prev = best.get(qqid)
            if prev is None or (len(records), fetched_at) > (
                len(prev["records"]),
                prev["fetched_at"],
            ):
                best[qqid] = {
                    "qqid": qqid,
                    "userinfo": userinfo,
                    "records": records,
                    "fetched_at": fetched_at,
                    "source": str(row["source"] or ""),
                }
        items = sorted(
            best.values(),
            key=lambda x: (len(x["records"]), x["fetched_at"]),
            reverse=True,
        )
        return items[: max(0, int(limit))]


class _LazyPlayerCacheDB:
    """延迟初始化 SQLite，避免插件 import 阶段阻塞启动。"""

    _db: Optional[PlayerCacheDB] = None

    def _inst(self) -> PlayerCacheDB:
        if self._db is None:
            self._db = PlayerCacheDB()
        return self._db

    def __getattr__(self, name: str):
        return getattr(self._inst(), name)


player_cache_db = _LazyPlayerCacheDB()


def _score_records_to_playinfo_dev(records: List[ScoreRecord]) -> List[PlayInfoDev]:
    out: List[PlayInfoDev] = []
    for r in records:
        lv = r.level or ""
        out.append(
            PlayInfoDev(
                song_id=int(r.song_id),
                title=r.title or "",
                level=lv,
                level_label=lv,
                level_index=int(r.level_index),
                achievements=float(r.achievements),
                fc=r.fc or "",
                fs=r.fs or "",
                type="SD",
                ds=round(float(r.ds), 1),
                dxScore=int(r.dxScore or 0),
                ra=int(r.ra),
                rate=r.rate or "",
            )
        )
    return out


def _bundle_from_snapshot(snap: DailySnapshot) -> CachedPlayerBundle:
    userinfo = UserInfo(
        nickname=snap.nickname,
        rating=int(snap.rating or 0),
        additional_rating=0,
        username=snap.nickname or "",
        charts=None,
    )
    records = _score_records_to_playinfo_dev(list(snap.records))
    stored_at = snap.stored_at or f"{snap.date}T00:00:00"
    try:
        fetched_at = time.mktime(time.strptime(stored_at[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        fetched_at = time.time()
    return CachedPlayerBundle(
        userinfo=userinfo,
        records=records,
        fetched_at=fetched_at,
        from_storage=True,
    )


def _try_storage_snapshot(qqid: int, max_age: int) -> Optional[CachedPlayerBundle]:
    if not _use_storage_fallback() or max_age <= 0:
        return None
    metas = data_storage.list_snapshots(qqid, limit=1)
    if not metas:
        return None
    sid = metas[0].get("snapshot_id", "")
    if not sid:
        return None
    snap = data_storage.load_snapshot_by_id(qqid, sid)
    if not snap or not snap.records:
        return None
    bundle = _bundle_from_snapshot(snap)
    if time.time() - bundle.fetched_at > max_age:
        return None
    log.debug(
        f"[PlayerCache] 使用数据存储快照 qq={qqid} date={snap.date} records={len(snap.records)}"
    )
    return bundle


def invalidate_player_cache(qqid: int) -> None:
    """清除某 QQ 的所有玩家缓存（各数据源），下次查询强制走查分器。"""
    try:
        removed = player_cache_db.delete_by_qqid(int(qqid))
        if removed:
            log.info(f"[PlayerCache] 已清除 qq={qqid} 的 {removed} 条缓存")
    except Exception as e:
        log.warning(f"[PlayerCache] 清除缓存失败 qq={qqid}: {e}")


def get_cached_player(
    qqid: Optional[int],
    username: Optional[str],
    source: str,
    *,
    force_refresh: bool = False,
) -> Optional[CachedPlayerBundle]:
    """读取有效缓存（SQLite → 可选数据存储快照）。"""
    if force_refresh:
        return None
    ttl = _cache_ttl_seconds()
    hit = player_cache_db.get(qqid, username, source, ttl)
    if hit is not None:
        log.debug(
            f"[PlayerCache] 命中 SQLite qq={qqid} user={username} source={source} "
            f"age={int(time.time() - hit.fetched_at)}s"
        )
        _set_fetch_meta(hit.fetched_at, "sqlite_cache")
        return hit
    if qqid and not username:
        snap_hit = _try_storage_snapshot(qqid, _storage_fallback_ttl_seconds())
        if snap_hit is not None:
            _set_fetch_meta(
                snap_hit.fetched_at,
                "storage_snapshot",
            )
        return snap_hit
    return None


def save_cached_player(
    qqid: Optional[int],
    username: Optional[str],
    source: str,
    userinfo: UserInfo,
    records: List[PlayInfoDev],
) -> None:
    if _cache_ttl_seconds() <= 0:
        return
    # 合并旧缓存：避免「先拉全量、后拉 B50」时互相覆盖 charts / records。
    # 只合并未过期的缓存，防止把过期的旧成绩「复活」成新数据。
    prev = player_cache_db.get(qqid, username, source, _cache_ttl_seconds())
    if prev is not None:
        if userinfo.charts is None and prev.userinfo.charts is not None:
            userinfo = prev.userinfo.model_copy(
                update={
                    "nickname": userinfo.nickname or prev.userinfo.nickname,
                    "rating": userinfo.rating or prev.userinfo.rating,
                    "username": userinfo.username or prev.userinfo.username,
                    "additional_rating": userinfo.additional_rating
                    if userinfo.additional_rating is not None
                    else prev.userinfo.additional_rating,
                }
            )
        if not records and prev.records:
            records = prev.records
    player_cache_db.set(qqid, username, source, userinfo, records)


async def resolve_player_records(
    qqid: Optional[int],
    username: Optional[str],
    source: str,
    fetch_fn,
    *,
    force_refresh: bool = False,
) -> Tuple[UserInfo, List[PlayInfoDev]]:
    """
    统一解析：先缓存，再 fetch_fn()，成功后写回缓存。
    fetch_fn 应返回 (userinfo, records)。
    """
    cached = get_cached_player(qqid, username, source, force_refresh=force_refresh)
    if cached is not None and cached.records:
        return cached.userinfo, cached.records
    userinfo, records = await fetch_fn()
    save_cached_player(qqid, username, source, userinfo, records)
    _set_fetch_meta(time.time(), "api", force_refresh=force_refresh)
    # 全量成绩到手后静默写贡献快照（尊重 data_share opt-out；不强制开启个人存储）
    if qqid and records:
        try:
            from .maimaidx_share_snapshot import maybe_save_share_snapshot

            maybe_save_share_snapshot(
                int(qqid), userinfo, records, source="share_query"
            )
        except Exception as e:
            log.debug(f"[ShareSnapshot] query hook skip qq={qqid}: {e}")
    return userinfo, records


async def resolve_player_b50(
    qqid: Optional[int],
    username: Optional[str],
    source: str,
    fetch_b50_fn,
    *,
    force_refresh: bool = False,
) -> UserInfo:
    """获取 B50：缓存中已有 charts 则直接返回，否则请求 API 并写回缓存。"""
    from .maimaidx_best_50 import regroup_b50_userinfo

    cached = get_cached_player(qqid, username, source, force_refresh=force_refresh)
    if cached is not None and cached.userinfo.charts is not None:
        return regroup_b50_userinfo(cached.userinfo)
    userinfo = regroup_b50_userinfo(await fetch_b50_fn())
    # 只复用 SQLite 缓存中的近期 records；存档快照数据不能重新盖新时间戳
    records = (
        cached.records if (cached is not None and not cached.from_storage) else []
    )
    save_cached_player(qqid, username, source, userinfo, records)
    _set_fetch_meta(time.time(), "api", force_refresh=force_refresh)
    return userinfo
