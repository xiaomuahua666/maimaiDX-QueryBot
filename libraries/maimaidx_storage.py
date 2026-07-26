"""QueryBot 统一持久化快照：SQLite 本地、YAML 或 MySQL。"""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PLUGIN_ROOT / "data"
STATE_DIR = DATA_ROOT / "storage"
MARKER_PATH = STATE_DIR / "active_backend.json"
FILE_INDEX_PATH = STATE_DIR / "local_file_index.json"
PENDING_PATH = STATE_DIR / "pending_restore.yaml"
STATIC_STATE_FILES = {
    "local_music_alias.json",
    "group_guess_switch.json",
    "group_guess_score.json",
    "group_guess_score_history.json",
    "group_guess_score_events.json",
    "group_guess_boost_cards.json",
    "group_guess_sync_prefs.json",
    "group_letter_stats.json",
    "group_alias_switch.json",
    "group_feature_switch.json",
}
REBUILDABLE_CACHE_DIRS = {
    ("audio_guess", "cache"),
    ("chart_guess", "cache"),
}


class StorageError(RuntimeError):
    pass


def _setting(config: Any, name: str, default: Any = None) -> Any:
    return getattr(config, name, default)


def backend_name(config: Any) -> str:
    value = str(_setting(config, "maimaidx_storage_backend", "sqlite") or "sqlite").lower()
    if value not in {"sqlite", "yaml", "mysql"}:
        raise StorageError("MAIMAIDX_STORAGE_BACKEND 只能是 sqlite、yaml 或 mysql")
    return value


def _yaml_path(config: Any) -> Path:
    value = str(_setting(config, "maimaidx_storage_yaml_path", "data/storage/state.yaml"))
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (PLUGIN_ROOT / path).resolve()


def _file_bytes(path: Path) -> bytes:
    if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        return path.read_bytes()
    # SQLite 在线备份可在 Bot 运行中取得一致快照。
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        target = sqlite3.connect(tmp.name)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        return Path(tmp.name).read_bytes()


def _manifest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        raw = files[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
        digest.update(str(len(raw)).encode())
    return digest.hexdigest()


def _excluded(path: Path, config: Any) -> bool:
    try:
        if path.resolve() == _yaml_path(config):
            return True
    except (OSError, RuntimeError):
        pass
    if (
        path.name == ".DS_Store"
        or path.suffix in {".tmp", ".lock"}
        or path.name.endswith(("-wal", "-shm", "-journal"))
    ):
        return True
    try:
        rel = path.resolve().relative_to(DATA_ROOT.resolve())
    except ValueError:
        return False
    if rel.parts and rel.parts[0] in {"storage", "migration"}:
        return True
    if len(rel.parts) >= 2 and rel.parts[:2] in REBUILDABLE_CACHE_DIRS:
        return True
    if not bool(_setting(config, "maimaidx_storage_include_user_scores", True)):
        if rel.parts and rel.parts[0] == "user_scores":
            return True
    if not bool(_setting(config, "maimaidx_storage_include_player_cache", True)):
        if rel.parts and rel.parts[0] == "player_cache":
            return True
    return False


def _excluded_directory(path: Path, config: Any) -> bool:
    """在遍历前剪枝，避免连可重建的数 GiB 缓存都逐文件扫描。"""
    try:
        rel = path.resolve().relative_to(DATA_ROOT.resolve())
    except ValueError:
        return False
    if rel.parts and rel.parts[0] in {"storage", "migration"}:
        return True
    if len(rel.parts) >= 2 and rel.parts[:2] in REBUILDABLE_CACHE_DIRS:
        return True
    if rel.parts and rel.parts[0] == "user_scores":
        return not bool(_setting(config, "maimaidx_storage_include_user_scores", True))
    if rel.parts and rel.parts[0] == "player_cache":
        return not bool(_setting(config, "maimaidx_storage_include_player_cache", True))
    return False


def _managed_files(config: Any) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if DATA_ROOT.exists():
        for root, dirs, files in os.walk(DATA_ROOT):
            root_path = Path(root)
            dirs[:] = sorted(
                name
                for name in dirs
                if not _excluded_directory(root_path / name, config)
            )
            for name in sorted(files):
                path = root_path / name
                if not _excluded(path, config):
                    result["data/" + path.relative_to(DATA_ROOT).as_posix()] = path
    static_value = str(_setting(config, "maimaidxpath", "") or "")
    if static_value:
        static_root = Path(static_value).expanduser().resolve()
        for name in STATIC_STATE_FILES:
            path = static_root / name
            if path.is_file():
                result["static/" + name] = path
    return result


def local_change_token(config: Any) -> str:
    """用路径、大小和纳秒 mtime 检测工作集是否需要重新制作快照。

    SQLite WAL 不进入持久化文件列表，但必须参与变更检测；在线 backup 会在真正
    同步时把 WAL 中已提交的数据合并进快照。
    """
    digest = hashlib.sha256()
    for logical, path in sorted(_managed_files(config).items()):
        candidates = [(logical, path)]
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            candidates.append((logical + "-wal", Path(str(path) + "-wal")))
        for label, candidate in candidates:
            digest.update(label.encode("utf-8"))
            digest.update(b"\0")
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                digest.update(b"missing")
                continue
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b":")
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> str:
    """单文件变更指纹（含 SQLite WAL/SHM），用于跳过重复读盘。"""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "missing"
    parts = [str(stat.st_size), str(stat.st_mtime_ns)]
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            try:
                side_stat = side.stat()
                parts.extend([suffix, str(side_stat.st_size), str(side_stat.st_mtime_ns)])
            except FileNotFoundError:
                parts.extend([suffix, "missing"])
    return ":".join(parts)


def _read_marker() -> dict[str, Any]:
    if not MARKER_PATH.is_file():
        return {}
    try:
        data = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_file_index() -> dict[str, Any]:
    if not FILE_INDEX_PATH.is_file():
        return {}
    try:
        data = json.loads(FILE_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_file_index(index: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FILE_INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(FILE_INDEX_PATH)


def _manifest_from_meta(files: dict[str, tuple[str, int]]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        sha_hex, size = files[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha_hex))
        digest.update(str(size).encode())
    return digest.hexdigest()


def collect_local_snapshot(config: Any) -> dict[str, Any]:
    files = {name: _file_bytes(path) for name, path in _managed_files(config).items()}
    created = time.time()
    return {
        "version": 1,
        "created_at": created,
        "files": files,
        "manifest": _manifest(files),
        "file_count": len(files),
        "total_bytes": sum(len(value) for value in files.values()),
    }


def _plan_local_files(config: Any) -> tuple[list[tuple[str, str, int, Optional[bytes]]], dict[str, Any]]:
    """按指纹复用未变文件，避免每次同步都把数 GiB 读进内存。

    返回 (name, sha256, size, raw_or_None) 列表与新的本地文件索引。
    raw 为 None 表示内容未变，MySQL 侧可从上一活动快照复制。
    """
    index = _load_file_index()
    new_index: dict[str, Any] = {}
    planned: list[tuple[str, str, int, Optional[bytes]]] = []
    for name, path in sorted(_managed_files(config).items()):
        fp = _file_fingerprint(path)
        cached = index.get(name) if isinstance(index.get(name), dict) else None
        if (
            cached
            and cached.get("fp") == fp
            and isinstance(cached.get("sha256"), str)
            and len(str(cached.get("sha256"))) == 64
            and cached.get("size") is not None
        ):
            sha = str(cached["sha256"])
            size = int(cached["size"])
            raw: Optional[bytes] = None
        else:
            raw = _file_bytes(path)
            sha = hashlib.sha256(raw).hexdigest()
            size = len(raw)
            fp = _file_fingerprint(path)
        new_index[name] = {"fp": fp, "sha256": sha, "size": size}
        planned.append((name, sha, size, raw))
    return planned, new_index


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    try:
        version = int(snapshot.get("version", 0))
        file_count = int(snapshot.get("file_count", -1))
        total_bytes = int(snapshot.get("total_bytes", -1))
    except (TypeError, ValueError) as exc:
        raise StorageError("快照元数据格式不正确") from exc
    if version != 1:
        raise StorageError("快照版本不受支持")
    files = snapshot.get("files")
    if not isinstance(files, dict):
        raise StorageError("快照缺少 files")
    for name, raw in files.items():
        if not isinstance(name, str) or not isinstance(raw, bytes):
            raise StorageError("快照文件格式不正确")
        parts = Path(name).parts
        if ".." in parts or not parts or parts[0] not in {"data", "static"}:
            raise StorageError(f"快照包含非法路径：{name}")
    actual = _manifest(files)
    if snapshot.get("manifest") != actual:
        raise StorageError("快照 SHA-256 校验失败，数据可能不完整")
    if file_count != len(files):
        raise StorageError("快照文件数量校验失败")
    if total_bytes != sum(len(raw) for raw in files.values()):
        raise StorageError("快照总字节数校验失败")


def _yaml_dump(snapshot: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise StorageError("YAML 后端需要安装 PyYAML") from exc
    payload = {
        "version": 1,
        "created_at": snapshot["created_at"],
        "manifest": snapshot["manifest"],
        "files": {
            name: {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "data": base64.b64encode(raw).decode("ascii"),
            }
            for name, raw in snapshot["files"].items()
        },
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=True)


def _yaml_load_text(text: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise StorageError("YAML 后端需要安装 PyYAML") from exc
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise StorageError("YAML 快照内容为空或格式错误")
    files: dict[str, bytes] = {}
    for name, item in (payload.get("files") or {}).items():
        try:
            raw = base64.b64decode(str(item["data"]), validate=True)
        except Exception as exc:
            raise StorageError(f"YAML 文件 {name} 的 Base64 无效") from exc
        try:
            expected_size = int(item.get("size", -1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise StorageError(f"YAML 文件 {name} 的大小格式无效") from exc
        if len(raw) != expected_size:
            raise StorageError(f"YAML 文件 {name} 的大小不匹配")
        if hashlib.sha256(raw).hexdigest() != str(item.get("sha256", "")):
            raise StorageError(f"YAML 文件 {name} 的 SHA-256 不匹配")
        files[str(name)] = raw
    snapshot = {
        "version": int(payload.get("version", 0)),
        "created_at": float(payload.get("created_at", 0)),
        "manifest": str(payload.get("manifest", "")),
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(len(raw) for raw in files.values()),
    }
    validate_snapshot(snapshot)
    return snapshot


def load_yaml_snapshot(config: Any) -> dict[str, Any]:
    path = _yaml_path(config)
    if not path.is_file():
        raise StorageError(f"YAML 快照不存在：{path}；请先执行存储迁移")
    return _yaml_load_text(path.read_text(encoding="utf-8"))


def save_yaml_snapshot(config: Any, snapshot: dict[str, Any]) -> None:
    validate_snapshot(snapshot)
    path = _yaml_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _yaml_dump(snapshot)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    backup = path.with_name(path.name + ".bak")
    try:
        tmp.write_text(text, encoding="utf-8")
        _yaml_load_text(tmp.read_text(encoding="utf-8"))
        if path.exists():
            shutil.copy2(path, backup)
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _mysql_params(config: Any) -> dict[str, Any]:
    host = str(_setting(config, "maimaidx_storage_mysql_host", "") or "").strip()
    user = str(_setting(config, "maimaidx_storage_mysql_user", "") or "").strip()
    database = str(_setting(config, "maimaidx_storage_mysql_database", "") or "").strip()
    missing = [name for name, value in (("HOST", host), ("USER", user), ("DATABASE", database)) if not value]
    if missing:
        names = ", ".join("MAIMAIDX_STORAGE_MYSQL_" + item for item in missing)
        raise StorageError(f"MySQL 地址配置不完整，缺少：{names}")
    try:
        port = int(_setting(config, "maimaidx_storage_mysql_port", 3306))
    except (TypeError, ValueError) as exc:
        raise StorageError("MAIMAIDX_STORAGE_MYSQL_PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise StorageError("MAIMAIDX_STORAGE_MYSQL_PORT 必须在 1 到 65535 之间")
    params: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": str(_setting(config, "maimaidx_storage_mysql_password", "") or ""),
        "database": database,
        "charset": str(_setting(config, "maimaidx_storage_mysql_charset", "utf8mb4")),
        "connect_timeout": 10,
        "autocommit": False,
    }
    if bool(_setting(config, "maimaidx_storage_mysql_ssl", False)):
        params["ssl"] = {}
    return params


def _mysql_prefix(config: Any) -> str:
    prefix = str(_setting(config, "maimaidx_storage_mysql_table_prefix", "maimaidx_") or "maimaidx_")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", prefix):
        raise StorageError("MAIMAIDX_STORAGE_MYSQL_TABLE_PREFIX 只能包含字母、数字和下划线")
    return prefix


def _mysql_connect(config: Any):
    try:
        import pymysql
    except ImportError as exc:
        raise StorageError("MySQL 后端需要安装 PyMySQL") from exc
    params = _mysql_params(config)
    try:
        return pymysql.connect(**params)
    except Exception as exc:
        raise StorageError(f"MySQL 连接失败：{type(exc).__name__}: {exc}") from exc


def _mysql_schema(conn: Any, prefix: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{prefix}storage_snapshots` (
            snapshot_id VARCHAR(64) PRIMARY KEY, namespace VARCHAR(128) NOT NULL,
            created_at DOUBLE NOT NULL, manifest CHAR(64) NOT NULL,
            file_count INT NOT NULL, total_bytes BIGINT NOT NULL,
            INDEX idx_ns_created(namespace, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{prefix}storage_files` (
            snapshot_id VARCHAR(64) NOT NULL, path VARCHAR(640) NOT NULL,
            sha256 CHAR(64) NOT NULL, size BIGINT NOT NULL, content LONGBLOB NOT NULL,
            PRIMARY KEY(snapshot_id, path),
            CONSTRAINT `{prefix}storage_files_fk` FOREIGN KEY(snapshot_id)
              REFERENCES `{prefix}storage_snapshots`(snapshot_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{prefix}storage_active` (
            namespace VARCHAR(128) PRIMARY KEY, snapshot_id VARCHAR(64) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    conn.commit()


def check_mysql(config: Any) -> None:
    conn = _mysql_connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.rollback()
    finally:
        conn.close()


def _mysql_active_ids(cur: Any, prefix: str, namespace: str) -> tuple[Optional[str], Optional[str]]:
    cur.execute(
        f"SELECT a.snapshot_id,s.manifest FROM `{prefix}storage_active` a "
        f"JOIN `{prefix}storage_snapshots` s ON s.snapshot_id=a.snapshot_id "
        "WHERE a.namespace=%s",
        (namespace,),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    return str(row[0]), str(row[1])


def _mysql_insert_files(
    cur: Any,
    prefix: str,
    snapshot_id: str,
    previous_id: Optional[str],
    planned: list[tuple[str, str, int, Optional[bytes]]],
    *,
    managed_paths: Optional[dict[str, Path]] = None,
) -> tuple[int, int]:
    """写入文件行；未变内容优先从上一快照复制，避免重复上传 BLOB。"""
    uploaded = 0
    reused = 0
    for name, sha, size, raw in planned:
        if raw is None and previous_id:
            cur.execute(
                f"INSERT INTO `{prefix}storage_files` (snapshot_id,path,sha256,size,content) "
                f"SELECT %s,path,sha256,size,content FROM `{prefix}storage_files` "
                "WHERE snapshot_id=%s AND path=%s AND sha256=%s AND size=%s",
                (snapshot_id, previous_id, name, sha, size),
            )
            if cur.rowcount:
                reused += 1
                continue
        if raw is None:
            if not managed_paths or name not in managed_paths:
                raise StorageError(f"MySQL 无法复用未变文件：{name}")
            raw = _file_bytes(managed_paths[name])
            sha = hashlib.sha256(raw).hexdigest()
            size = len(raw)
        cur.execute(
            f"INSERT INTO `{prefix}storage_files` VALUES (%s,%s,%s,%s,%s)",
            (snapshot_id, name, sha, size, raw),
        )
        uploaded += 1
    return uploaded, reused


def save_mysql_snapshot(config: Any, snapshot: dict[str, Any]) -> None:
    validate_snapshot(snapshot)
    planned = [
        (name, hashlib.sha256(raw).hexdigest(), len(raw), raw)
        for name, raw in sorted(snapshot["files"].items())
    ]
    _save_mysql_planned(
        config,
        planned,
        created_at=float(snapshot["created_at"]),
        manifest=str(snapshot["manifest"]),
        file_count=int(snapshot["file_count"]),
        total_bytes=int(snapshot["total_bytes"]),
    )


def _save_mysql_planned(
    config: Any,
    planned: list[tuple[str, str, int, Optional[bytes]]],
    *,
    created_at: float,
    manifest: str,
    file_count: int,
    total_bytes: int,
    managed_paths: Optional[dict[str, Path]] = None,
) -> dict[str, int]:
    namespace = str(_setting(config, "maimaidx_storage_namespace", "default") or "default")
    prefix = _mysql_prefix(config)
    snapshot_id = uuid.uuid4().hex
    conn = _mysql_connect(config)
    uploaded = reused = 0
    try:
        _mysql_schema(conn, prefix)
        with conn.cursor() as cur:
            previous_id, current_manifest = _mysql_active_ids(cur, prefix, namespace)
            if current_manifest == manifest:
                conn.rollback()
                return {"uploaded": 0, "reused": 0, "skipped": 1}
            cur.execute(
                f"INSERT INTO `{prefix}storage_snapshots` VALUES (%s,%s,%s,%s,%s,%s)",
                (snapshot_id, namespace, created_at, manifest, file_count, total_bytes),
            )
            uploaded, reused = _mysql_insert_files(
                cur, prefix, snapshot_id, previous_id, planned, managed_paths=managed_paths
            )
            cur.execute(
                f"INSERT INTO `{prefix}storage_active` VALUES (%s,%s) "
                "ON DUPLICATE KEY UPDATE snapshot_id=VALUES(snapshot_id)",
                (namespace, snapshot_id),
            )
        keep = max(1, int(_setting(config, "maimaidx_storage_mysql_keep_snapshots", 3)))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT snapshot_id FROM `{prefix}storage_snapshots` "
                "WHERE namespace=%s AND snapshot_id<>%s ORDER BY created_at DESC",
                (namespace, snapshot_id),
            )
            stale = [row[0] for row in cur.fetchall()[max(0, keep - 1):]]
            if stale:
                placeholders = ",".join(["%s"] * len(stale))
                cur.execute(
                    f"DELETE FROM `{prefix}storage_snapshots` WHERE snapshot_id IN ({placeholders})",
                    stale,
                )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise StorageError(f"MySQL 写入失败，旧快照未切换：{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()
    expected = {
        "manifest": manifest,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    _verify_mysql_active_snapshot(config, expected)
    return {"uploaded": uploaded, "reused": reused, "skipped": 0}


def _verify_mysql_active_snapshot(config: Any, expected: dict[str, Any]) -> None:
    """校验活动快照元数据与逐文件 sha/size，不再回拉整包 BLOB。"""
    namespace = str(_setting(config, "maimaidx_storage_namespace", "default") or "default")
    prefix = _mysql_prefix(config)
    conn = _mysql_connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT a.snapshot_id,s.manifest,s.file_count,s.total_bytes "
                f"FROM `{prefix}storage_active` a "
                f"JOIN `{prefix}storage_snapshots` s ON s.snapshot_id=a.snapshot_id "
                "WHERE a.namespace=%s",
                (namespace,),
            )
            meta = cur.fetchone()
            if not meta:
                raise StorageError("MySQL 写入后活动快照不存在")
            snapshot_id, manifest, file_count, total_bytes = meta
            if (
                str(manifest) != expected["manifest"]
                or int(file_count) != expected["file_count"]
                or int(total_bytes) != expected["total_bytes"]
            ):
                raise StorageError("MySQL 写入后元数据校验失败")
            cur.execute(
                f"SELECT path,sha256,size FROM `{prefix}storage_files` "
                "WHERE snapshot_id=%s ORDER BY path",
                (snapshot_id,),
            )
            rows = cur.fetchall()
        meta_map = {str(name): (str(sha256), int(size)) for name, sha256, size in rows}
        if len(meta_map) != expected["file_count"]:
            raise StorageError("MySQL 写入后文件数量校验失败")
        if sum(size for _, size in meta_map.values()) != expected["total_bytes"]:
            raise StorageError("MySQL 写入后总字节数校验失败")
        if _manifest_from_meta(meta_map) != expected["manifest"]:
            raise StorageError("MySQL 写入后内容校验失败")
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(f"MySQL 写入后校验失败：{type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


def load_mysql_snapshot(config: Any) -> dict[str, Any]:
    namespace = str(_setting(config, "maimaidx_storage_namespace", "default") or "default")
    prefix = _mysql_prefix(config)
    conn = _mysql_connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT snapshot_id FROM `{prefix}storage_active` WHERE namespace=%s", (namespace,))
            active = cur.fetchone()
            if not active:
                raise StorageError(f"MySQL 命名空间 {namespace} 尚无快照；请先执行存储迁移")
            snapshot_id = active[0]
            cur.execute(
                f"SELECT created_at,manifest,file_count,total_bytes FROM `{prefix}storage_snapshots` WHERE snapshot_id=%s",
                (snapshot_id,),
            )
            meta = cur.fetchone()
            if not meta:
                raise StorageError(f"MySQL 活动快照 {snapshot_id} 缺少元数据")
            cur.execute(
                f"SELECT path,sha256,size,content FROM `{prefix}storage_files` WHERE snapshot_id=%s",
                (snapshot_id,),
            )
            files: dict[str, bytes] = {}
            for name, sha256, size, content in cur.fetchall():
                raw = bytes(content)
                if len(raw) != int(size) or hashlib.sha256(raw).hexdigest() != sha256:
                    raise StorageError(f"MySQL 文件 {name} 校验失败")
                files[str(name)] = raw
        snapshot = {
            "version": 1, "created_at": float(meta[0]), "manifest": str(meta[1]),
            "file_count": int(meta[2]), "total_bytes": int(meta[3]), "files": files,
        }
        validate_snapshot(snapshot)
        return snapshot
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(
            f"MySQL 快照表不可用：{type(exc).__name__}: {exc}；请先执行存储迁移"
        ) from exc
    finally:
        conn.close()


def load_snapshot(config: Any, backend: str) -> dict[str, Any]:
    if backend == "sqlite":
        return collect_local_snapshot(config)
    if backend == "yaml":
        return load_yaml_snapshot(config)
    if backend == "mysql":
        return load_mysql_snapshot(config)
    raise StorageError(f"未知存储后端：{backend}")


def save_snapshot(config: Any, backend: str, snapshot: dict[str, Any]) -> str:
    if backend == "yaml":
        save_yaml_snapshot(config, snapshot)
        return "YAML 快照已原子写入并校验"
    if backend == "mysql":
        save_mysql_snapshot(config, snapshot)
        return "MySQL 新快照已事务写入并校验"
    if backend == "sqlite":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(_yaml_dump(snapshot), encoding="utf-8")
        try:
            os.chmod(PENDING_PATH, 0o600)
        except OSError:
            pass
        _yaml_load_text(PENDING_PATH.read_text(encoding="utf-8"))
        return "SQLite 恢复任务已暂存；重启 Bot 后应用（运行中不会覆盖已打开数据库）"
    raise StorageError(f"未知存储后端：{backend}")


def check_target(config: Any, backend: str) -> None:
    if backend == "mysql":
        check_mysql(config)
    elif backend == "yaml":
        path = _yaml_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(path.parent, os.W_OK):
            raise StorageError(f"YAML 目录不可写：{path.parent}")
    elif backend == "sqlite":
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not os.access(STATE_DIR, os.W_OK):
            raise StorageError(f"SQLite 恢复目录不可写：{STATE_DIR}")
    else:
        raise StorageError(f"未知存储后端：{backend}")


def _target_path(config: Any, logical: str) -> Path:
    root, _, relative = logical.partition("/")
    if root == "data":
        base = DATA_ROOT.resolve()
    elif root == "static":
        value = str(_setting(config, "maimaidxpath", "") or "")
        if not value:
            raise StorageError("恢复 static 状态需要配置 MAIMAIDXPATH")
        base = Path(value).expanduser().resolve()
    else:
        raise StorageError(f"非法快照路径：{logical}")
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise StorageError(f"快照路径越界：{logical}")
    return target


def restore_snapshot(config: Any, snapshot: dict[str, Any]) -> None:
    validate_snapshot(snapshot)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    backup_root = Path(tempfile.mkdtemp(prefix="restore-backup-", dir=STATE_DIR))
    replaced: list[tuple[Path, Optional[Path]]] = []
    try:
        # 目标中多出的受管状态也要备份并删除，避免旧记录与源快照混合。
        for logical, target in _managed_files(config).items():
            if logical in snapshot["files"]:
                continue
            if target.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = Path(str(target) + suffix)
                    if sidecar.exists():
                        sidecar_backup = backup_root / str(len(replaced))
                        shutil.copy2(sidecar, sidecar_backup)
                        sidecar.unlink()
                        replaced.append((sidecar, sidecar_backup))
            backup = backup_root / str(len(replaced))
            shutil.copy2(target, backup)
            target.unlink()
            replaced.append((target, backup))
        for logical, raw in snapshot["files"].items():
            target = _target_path(config, logical)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = Path(str(target) + suffix)
                    if sidecar.exists():
                        sidecar_backup = backup_root / str(len(replaced))
                        shutil.copy2(sidecar, sidecar_backup)
                        sidecar.unlink()
                        replaced.append((sidecar, sidecar_backup))
            backup = None
            if target.exists():
                backup = backup_root / str(len(replaced))
                shutil.copy2(target, backup)
            fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            replaced.append((target, backup))
    except Exception as exc:
        for target, backup in reversed(replaced):
            if backup and backup.exists():
                shutil.copy2(backup, target)
            elif target.exists():
                target.unlink()
        raise StorageError(f"恢复失败，已回滚已替换文件：{type(exc).__name__}: {exc}") from exc
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def _write_marker(
    config: Any,
    backend: str,
    manifest: str,
    *,
    change_token: Optional[str] = None,
    file_count: int = 0,
    total_bytes: int = 0,
    created_at: Optional[float] = None,
) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "backend": backend,
        "namespace": str(_setting(config, "maimaidx_storage_namespace", "default")),
        "manifest": manifest,
        "file_count": int(file_count),
        "total_bytes": int(total_bytes),
        "updated_at": time.time(),
        "created_at": float(created_at if created_at is not None else time.time()),
    }
    if change_token:
        payload["change_token"] = change_token
    MARKER_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _trim_process_memory() -> None:
    gc.collect()
    if os.name == "posix":
        try:
            import ctypes

            trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
            if trim is not None:
                trim(0)
        except (OSError, TypeError):
            pass


def sync_configured_backend(config: Any) -> Optional[dict[str, Any]]:
    backend = backend_name(config)
    if backend == "sqlite":
        return None
    namespace = str(_setting(config, "maimaidx_storage_namespace", "default") or "default")
    change_token = local_change_token(config)
    marker = _read_marker()
    if (
        marker.get("backend") == backend
        and str(marker.get("namespace") or "") == namespace
        and marker.get("change_token") == change_token
        and marker.get("manifest")
    ):
        return {
            "version": 1,
            "created_at": float(marker.get("created_at") or marker.get("updated_at") or 0),
            "manifest": str(marker["manifest"]),
            "file_count": int(marker.get("file_count") or 0),
            "total_bytes": int(marker.get("total_bytes") or 0),
            "skipped": True,
        }

    if backend == "mysql":
        planned, new_index = _plan_local_files(config)
        created_at = time.time()
        meta = {name: (sha, size) for name, sha, size, _ in planned}
        manifest = _manifest_from_meta(meta)
        file_count = len(planned)
        total_bytes = sum(size for _, _, size, _ in planned)
        try:
            stats = _save_mysql_planned(
                config,
                planned,
                created_at=created_at,
                manifest=manifest,
                file_count=file_count,
                total_bytes=total_bytes,
                managed_paths=_managed_files(config),
            )
            _save_file_index(new_index)
            _write_marker(
                config,
                backend,
                manifest,
                change_token=change_token,
                file_count=file_count,
                total_bytes=total_bytes,
                created_at=created_at,
            )
            return {
                "version": 1,
                "created_at": created_at,
                "manifest": manifest,
                "file_count": file_count,
                "total_bytes": total_bytes,
                "uploaded": int(stats.get("uploaded") or 0),
                "reused": int(stats.get("reused") or 0),
                "skipped": bool(stats.get("skipped")),
            }
        finally:
            planned.clear()
            _trim_process_memory()

    snapshot = collect_local_snapshot(config)
    try:
        save_snapshot(config, backend, snapshot)
        _write_marker(
            config,
            backend,
            snapshot["manifest"],
            change_token=change_token,
            file_count=int(snapshot["file_count"]),
            total_bytes=int(snapshot["total_bytes"]),
            created_at=float(snapshot["created_at"]),
        )
        return {
            key: snapshot[key]
            for key in ("version", "created_at", "manifest", "file_count", "total_bytes")
        }
    finally:
        snapshot.clear()
        _trim_process_memory()


def bootstrap_storage(config: Any) -> str:
    """必须在各 SQLite 单例导入前调用。"""
    backend = backend_name(config)
    if PENDING_PATH.is_file():
        snapshot = _yaml_load_text(PENDING_PATH.read_text(encoding="utf-8"))
        restore_snapshot(config, snapshot)
        PENDING_PATH.unlink()
        _write_marker(config, "sqlite", snapshot["manifest"])
        return "已应用待处理 SQLite 恢复快照"
    if backend == "sqlite":
        return "使用原生 SQLite/JSON 本地存储"
    check_target(config, backend)
    namespace = str(_setting(config, "maimaidx_storage_namespace", "default"))
    marker = {}
    if MARKER_PATH.is_file():
        try:
            marker = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
        except Exception:
            marker = {}
    policy = str(_setting(config, "maimaidx_storage_bootstrap_policy", "auto") or "auto").lower()
    if policy not in {"auto", "remote"}:
        raise StorageError("MAIMAIDX_STORAGE_BOOTSTRAP_POLICY 只能是 auto 或 remote")
    if policy == "auto" and marker.get("backend") == backend and marker.get("namespace") == namespace:
        return f"{backend} 后端已关联，沿用本地工作缓存"
    try:
        snapshot = load_snapshot(config, backend)
    except StorageError as exc:
        if any(marker in str(exc) for marker in ("尚无快照", "快照不存在", "快照表不可用")):
            if bool(_setting(config, "maimaidx_storage_allow_empty_remote_init", False)):
                return f"{backend} 尚无快照，已允许从现有本地数据初始化"
            raise StorageError(
                f"{backend} 尚无快照；请先保持 BACKEND=sqlite 执行存储迁移，"
                "或明确设置 MAIMAIDX_STORAGE_ALLOW_EMPTY_REMOTE_INIT=true"
            ) from exc
        raise
    restore_snapshot(config, snapshot)
    _write_marker(config, backend, snapshot["manifest"])
    return f"已从 {backend} 恢复 {snapshot['file_count']} 个状态文件"
