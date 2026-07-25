#!/usr/bin/env python3
"""把导出的脱敏公开数据集上传到 Kaggle（API）。

依赖：
  pip install kaggle
  # 服务器： /www/bot/.venv/bin/pip install kaggle

凭证（优先 env，勿提交到 git）：
  1) KAGGLE_API_TOKEN=KGAT_xxx          # 推荐：新版 API token
  2) KAGGLE_USERNAME + KAGGLE_KEY       # 旧版 legacy key
  可写在 Bot 根目录 .env（已被 .gitignore），由本脚本 / run_upload_kaggle.sh 加载

典型用法：
  # 先把 token 放进 /www/bot/awmc/.env ，再：
  bash scripts/run_upload_kaggle.sh

  # 默认更新已有 dx-2026-awmcbot（version）
  python scripts/upload_public_dataset_to_kaggle.py
  python scripts/upload_public_dataset_to_kaggle.py --dry-run --keep-work
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]

# 常见本地 env 路径（绝不把真实 token 写进仓库）
# 优先 .env.kaggle，避免被 Bot 主 .env 里空值占坑
_DEFAULT_ENV_CANDIDATES = (
    Path("/www/bot/awmc/.env.kaggle"),
    Path("/www/bot/awmc/.env"),
    Path("/www/bot/awmc/.env.prod"),
    ROOT / ".env",
    ROOT / ".env.prod",
)


def _log(msg: str) -> None:
    print(f"[kaggle] {msg}", flush=True)


def _die(msg: str, code: int = 1) -> None:
    _log(f"ERROR: {msg}")
    raise SystemExit(code)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _mask_secret(value: str) -> str:
    v = (value or "").strip()
    if len(v) <= 10:
        return "***"
    return f"{v[:6]}...{v[-4:]} (len={len(v)})"


def _load_env_file(path: Path) -> int:
    """轻量加载 KEY=VALUE（不依赖 python-dotenv）。返回新写入条数。"""
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        # 不覆盖已在 shell 里显式 export 的值
        if key in os.environ and str(os.environ.get(key) or "").strip():
            continue
        os.environ[key] = val
        loaded += 1
    return loaded


def _load_env_files(explicit: Optional[Path]) -> None:
    seen: List[Path] = []
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_hint = (os.environ.get("KAGGLE_ENV_FILE") or "").strip()
    if env_hint:
        candidates.append(Path(env_hint))
    candidates.extend(_DEFAULT_ENV_CANDIDATES)
    for p in candidates:
        rp = p.expanduser().resolve() if p.exists() else p
        if rp in seen:
            continue
        seen.append(rp)
        n = _load_env_file(p)
        if n:
            _log(f"loaded env file={p} new_keys={n}")


def _prepare_kaggle_auth(
    *,
    kaggle_json: Optional[Path],
    owner_override: str = "",
) -> Tuple[str, str]:
    """准备凭证，返回 (username_hint, auth_mode)。

    username_hint 可能为空（KGAT token 需 authenticate 后 introspect）。
    绝不把 token/key 写入仓库或日志全文。
    """
    token = (os.environ.get("KAGGLE_API_TOKEN") or "").strip()
    username = (owner_override or os.environ.get("KAGGLE_USERNAME") or "").strip()
    key = (os.environ.get("KAGGLE_KEY") or "").strip()

    if token:
        if not token.startswith("KGAT_") and not token.startswith("KAGGLE_"):
            _log("WARN: KAGGLE_API_TOKEN 不像新版 token（通常以 KGAT_ 开头）")
        # 可选落盘供部分 CLI 读取；权限 0600；路径在 home，不在 git 仓库
        token_file = Path.home() / ".kaggle" / "access_token"
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token + "\n", encoding="utf-8")
            os.chmod(token_file, 0o600)
        except OSError as e:
            _log(f"WARN: cannot write ~/.kaggle/access_token: {e}")
        _log(f"auth_mode=api_token token={_mask_secret(token)}")
        return username, "api_token"

    cred_path = Path(kaggle_json) if kaggle_json else Path.home() / ".kaggle" / "kaggle.json"
    if username and key:
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            json.dumps({"username": username, "key": key}), encoding="utf-8"
        )
        try:
            os.chmod(cred_path, 0o600)
        except OSError:
            pass
        _log(f"auth_mode=legacy_env user={username} key={_mask_secret(key)}")
        return username, "legacy_env"

    if cred_path.exists():
        try:
            data = _load_json(cred_path)
        except Exception as e:
            _die(f"无法读取凭证 {cred_path}: {e}")
        username = str(data.get("username") or "").strip()
        key = str(data.get("key") or "").strip()
        if not username or not key:
            _die(f"凭证文件缺 username/key: {cred_path}")
        try:
            os.chmod(cred_path, 0o600)
        except OSError:
            pass
        os.environ.setdefault("KAGGLE_USERNAME", username)
        os.environ.setdefault("KAGGLE_KEY", key)
        _log(f"auth_mode=legacy_file user={username} path={cred_path}")
        return username, "legacy_file"

    _die(
        "找不到 Kaggle 凭证。请在 .env 中设置（勿提交 git）：\n"
        "  KAGGLE_API_TOKEN=KGAT_xxxxxxxx\n"
        "或旧版：\n"
        "  KAGGLE_USERNAME=...\n"
        "  KAGGLE_KEY=...\n"
        "模板见仓库 .env.example；服务器建议写 /www/bot/awmc/.env"
    )


def _import_kaggle_api():
    """获取已认证 API。

    新版 kaggle 在 import 时可能消费并删除 KAGGLE_API_TOKEN，
    因此优先使用包内预认证的 kaggle.api。
    """
    token = (os.environ.get("KAGGLE_API_TOKEN") or "").strip()
    # 备份，避免被 import 副作用清掉后无法排查
    if token:
        os.environ["KAGGLE_API_TOKEN"] = token

    try:
        import kaggle  # type: ignore
    except ImportError:
        _die(
            "未安装 kaggle 包。请执行：\n"
            "  pip install -U kaggle\n"
            "或服务器：\n"
            "  /www/bot/.venv/bin/pip install -U kaggle"
        )

    api = getattr(kaggle, "api", None)
    if api is not None:
        # 已在 import 时 authenticate
        try:
            # 触发一次轻量调用确认可用
            _ = api.get_config_value("username")
        except Exception:
            pass
        return api

    from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore

    # 若 token 已被 pop，尝试从 access_token 文件恢复到 env
    if not (os.environ.get("KAGGLE_API_TOKEN") or "").strip():
        tf = Path.home() / ".kaggle" / "access_token"
        if tf.exists():
            os.environ["KAGGLE_API_TOKEN"] = tf.read_text(encoding="utf-8").strip()
    api = KaggleApi()
    api.authenticate()
    return api


def _resolve_username(api, hint: str = "") -> str:
    if hint:
        return hint
    for getter in (
        lambda: api.get_config_value("username"),
        lambda: (getattr(api, "config_values", {}) or {}).get("username"),
        lambda: os.environ.get("KAGGLE_USERNAME"),
    ):
        try:
            u = (getter() or "").strip()
        except Exception:
            u = ""
        if u:
            return u
    _die(
        "无法从 token 解析 Kaggle 用户名。请在 .env 额外设置 KAGGLE_USERNAME=你的用户名"
    )


def _resolve_export_dir(export_dir: Optional[Path], data_root: Path) -> Path:
    if export_dir:
        p = Path(export_dir)
        if not p.exists():
            _die(f"export-dir 不存在: {p}")
        return p.resolve()

    latest = data_root / "public_dataset_latest"
    if latest.exists():
        return latest.resolve()

    cands = sorted(
        data_root.glob("public_dataset_*"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    cands = [c for c in cands if c.is_dir() and not c.is_symlink()]
    if cands:
        _log(f"auto-picked newest export-dir={cands[0]}")
        return cands[0].resolve()

    # 本地仓库回退
    local = ROOT / "data" / "public_dataset"
    if local.exists():
        return local.resolve()
    _die(
        f"找不到导出目录。请传 --export-dir，或先跑导出生成 "
        f"{data_root}/public_dataset_<时间戳>"
    )


def _stage_from_hf_upload(src: Path, staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(src, staging)


def _stage_from_zip(zip_path: Path, staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)
    _log(f"unzip {zip_path} -> {staging} members={len(zf.namelist())}")


def _stage_from_export_dir(export_dir: Path, staging: Path) -> None:
    """优先用 export 里已有的 hf_upload/；否则现场 pack。"""
    hf_dir = export_dir / "hf_upload"
    if (hf_dir / "data" / "players.jsonl").exists():
        _log(f"use existing hf_upload={hf_dir}")
        _stage_from_hf_upload(hf_dir, staging)
        return

    # 尝试调用同仓库 pack_hf_upload
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from export_public_dataset import pack_hf_upload  # type: ignore
    except Exception as e:
        _die(
            f"export-dir 无 hf_upload/，且无法 import pack_hf_upload: {e}\n"
            "请先完整跑一遍 export，或传 --zip / --upload-dir"
        )
    _log(f"packing hf_upload from export-dir={export_dir}")
    pack_hf_upload(export_dir)
    if not (hf_dir / "data" / "players.jsonl").exists():
        _die("pack_hf_upload 后仍缺少 data/players.jsonl")
    _stage_from_hf_upload(hf_dir, staging)


def _preflight_staging(staging: Path) -> Dict[str, Any]:
    required = [
        staging / "data" / "players.jsonl",
        staging / "dataset_meta.json",
    ]
    missing = [str(p.relative_to(staging)) for p in required if not p.exists()]
    if missing:
        _die(f"上传目录缺必要文件: {missing}")

    # 禁止带 salt / 根级 peer_stats（与 HF 包一致）
    banned = []
    for p in staging.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(staging).as_posix()
        name = p.name
        if name == ".dataset_salt" or rel in {"peer_stats.json", "peer_stats.json.gz"}:
            banned.append(rel)
        if name.endswith(".gz") and "peer_stats" in name:
            banned.append(rel)
    if banned:
        _die(f"上传目录含禁止文件（请用 hf_upload 结构）: {banned}")

    players = staging / "data" / "players.jsonl"
    n_players = sum(1 for _ in players.open(encoding="utf-8") if _.strip())
    meta = _load_json(staging / "dataset_meta.json")
    files = sorted(
        str(p.relative_to(staging).as_posix())
        for p in staging.rglob("*")
        if p.is_file() and p.name != "dataset-metadata.json"
    )
    total_bytes = sum((staging / f).stat().st_size for f in files)
    info = {
        "players_jsonl_lines": n_players,
        "meta_player_count": meta.get("player_count"),
        "meta_generated_at": meta.get("generated_at"),
        "file_count": len(files),
        "size_mb": round(total_bytes / 1024 / 1024, 2),
        "files": files,
    }
    _log(
        f"preflight OK players_lines={n_players} meta_players={info['meta_player_count']} "
        f"files={len(files)} size_mb={info['size_mb']}"
    )
    for f in files:
        _log(f"  + {f} ({(staging / f).stat().st_size} bytes)")
    return info


def _build_description(meta: dict, staging_info: dict) -> str:
    return (
        "# maimaiDX Desensitized Score Dataset\n\n"
        "Anonymized maimai DX player score snapshots for ARPI / roast-prompt research.\n\n"
        f"- players: {meta.get('player_count')} (opt-out excluded)\n"
        f"- generated_at: {meta.get('generated_at')}\n"
        f"- bucket_count: {meta.get('bucket_count')}\n"
        f"- upload_files: {staging_info.get('file_count')} "
        f"({staging_info.get('size_mb')} MB)\n\n"
        "## Files\n\n"
        "| Path | Description |\n"
        "|------|-------------|\n"
        "| `data/players.jsonl` | Anonymized players (B50 + full records + rating trends) |\n"
        "| `data/rating_trends.jsonl` | Rating trend samples |\n"
        "| `data/roast_training_samples.jsonl` | Lightweight roast/prompt samples |\n"
        "| `assets/arpi_peer_stats.json` | Peer ARPI stats by rating bucket |\n"
        "| `dataset_meta.json` / `quality_report.json` | Export metadata & QA |\n\n"
        "## Privacy\n\n"
        "- No QQ / nickname; `player_id` is a one-way hash\n"
        "- Users who sent 「不同意共享我的数据」 are excluded\n"
    )


def _write_dataset_metadata(
    staging: Path,
    *,
    dataset_id: str,
    title: str,
    subtitle: str,
    license_name: str,
    keywords: List[str],
    meta: dict,
    staging_info: dict,
) -> Path:
    payload = {
        "title": title,
        "id": dataset_id,
        "subtitle": subtitle[:80] if subtitle else "",
        "description": _build_description(meta, staging_info),
        "licenses": [{"name": license_name}],
        "keywords": keywords,
        "resources": [
            {
                "path": "data/players.jsonl",
                "description": "Anonymized player snapshots",
            },
            {
                "path": "assets/arpi_peer_stats.json",
                "description": "Peer ARPI statistics",
            },
        ],
    }
    out = staging / "dataset-metadata.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"wrote {out}")
    return out


def _dataset_exists(api, dataset_id: str) -> Optional[bool]:
    """返回 True/False；查不清时返回 None（不要误当成缺失去 create）。"""
    owner, _, slug = dataset_id.partition("/")
    if not owner or not slug:
        _die(f"非法 dataset id: {dataset_id}")

    # 1) list 用户数据集（对 token 更稳；status 常 403）
    try:
        try:
            rows = api.dataset_list(user=owner, search=slug)
        except TypeError:
            rows = api.dataset_list(user=owner)
        for r in rows or []:
            ref = str(getattr(r, "ref", "") or "").lower()
            if ref == dataset_id.lower() or ref.endswith("/" + slug.lower()):
                return True
        # 再扫一页无 search 的列表，避免 search 漏检
        try:
            rows2 = api.dataset_list(user=owner)
        except TypeError:
            rows2 = []
        for r in rows2 or []:
            ref = str(getattr(r, "ref", "") or "").lower()
            if ref == dataset_id.lower() or ref.endswith("/" + slug.lower()):
                return True
    except Exception as e:
        _log(f"dataset_list check failed: {type(e).__name__}: {e}")

    # 2) status 兜底
    try:
        api.dataset_status(dataset_id)
        return True
    except Exception as e:
        msg = str(e).lower()
        if "404" in msg or "not found" in msg or "does not exist" in msg:
            return False
        _log(f"dataset_exists inconclusive ({type(e).__name__}: {e})")
        return None


def _upload(
    api,
    staging: Path,
    *,
    mode: str,
    public: bool,
    message: str,
    dry_run: bool,
) -> str:
    meta_path = staging / "dataset-metadata.json"
    meta = _load_json(meta_path)
    dataset_id = meta["id"]
    exists = _dataset_exists(api, dataset_id)
    _log(f"dataset_id={dataset_id} exists={exists} mode={mode} public={public}")

    if dry_run:
        _log("dry-run: skip upload")
        return "dry-run"

    # Kaggle 对子目录默认 skip；我们需要 zip 打包 data/ assets/
    dir_mode = "zip"
    convert_to_csv = False

    if mode == "auto":
        if exists is True:
            mode = "version"
        elif exists is False:
            mode = "create"
        else:
            # 查不清时默认更新，避免误新建第二个 dataset
            mode = "version"
            _log("exists unknown -> prefer version (won't create)")
        _log(f"auto resolved mode={mode}")

    if mode == "create":
        if exists is True:
            _die(
                f"数据集已存在: {dataset_id}。请用 --mode version，或换 --slug。"
            )
        _log("creating new dataset ...")
        result = api.dataset_create_new(
            str(staging),
            public=public,
            quiet=False,
            convert_to_csv=convert_to_csv,
            dir_mode=dir_mode,
        )
    elif mode == "version":
        if exists is False:
            _die(
                f"数据集不存在，拒绝自动新建: {dataset_id}\n"
                "若确需新建请显式 MODE=create；否则检查 --slug 是否指到已有数据集。"
            )
        _log(f"creating new version notes={message!r} ...")
        result = api.dataset_create_version(
            str(staging),
            version_notes=message,
            quiet=False,
            convert_to_csv=convert_to_csv,
            dir_mode=dir_mode,
            delete_old_versions=False,
        )
    else:
        _die(f"未知 mode: {mode}")

    _log(f"api result={result!r}")
    url = f"https://www.kaggle.com/datasets/{dataset_id}"
    _log(f"dataset url={url}")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload public dataset to Kaggle via API")
    parser.add_argument(
        "--export-dir",
        type=Path,
        default=None,
        help="export_public_dataset 输出目录（含 hf_upload/ 或可现场打包）",
    )
    parser.add_argument(
        "--zip",
        type=Path,
        default=None,
        help="hf_upload_*.zip；若给了则优先用 zip 解压上传",
    )
    parser.add_argument(
        "--upload-dir",
        type=Path,
        default=None,
        help="已准备好的上传目录（含 data/）；跳过自动打包",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("/www/bot/awmc/data"),
        help="自动发现 public_dataset_latest 的根目录",
    )
    parser.add_argument(
        "--slug",
        default="dx-2026-awmcbot",
        help="已有数据集 slug（默认更新 awmcteam/dx-2026-awmcbot）",
    )
    parser.add_argument(
        "--title",
        default="舞萌DX 2026 AWMCBOT 用户成绩数据集",
    )
    parser.add_argument(
        "--subtitle",
        default="脱敏 B50 / 全量成绩 / Rating 趋势 / ARPI 同段统计",
    )
    parser.add_argument(
        "--license",
        default="CC-BY-NC-SA-4.0",
        help="Kaggle license name，如 CC0-1.0 / CC-BY-SA-4.0 / CC-BY-NC-SA-4.0",
    )
    parser.add_argument(
        "--keywords",
        default="games",
        help="Kaggle 官方 keywords（逗号分隔；无效 tag 会被丢弃，默认 games）",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "create", "version"),
        default="version",
        help="默认 version=更新已有数据集；create 仅新建；auto 查不清时也偏好 version",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="创建时设为公开（仅 create 生效；已有数据集隐私在网页改）",
    )
    parser.add_argument(
        "--message",
        default="",
        help="version notes；默认带时间戳与 player_count",
    )
    parser.add_argument("--kaggle-json", type=Path, default=None)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="加载含 KAGGLE_API_TOKEN 的 env 文件（默认自动找 /www/bot/awmc/.env 等）",
    )
    parser.add_argument(
        "--owner",
        default="",
        help="数据集 owner（默认从 token introspect 或 KAGGLE_USERNAME）",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="暂存上传目录（默认 data_root/kaggle_upload_<stamp>）",
    )
    parser.add_argument("--keep-work", action="store_true", help="保留暂存目录")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stamp = _stamp()
    _log(f"stamp={stamp}")
    _log(f"time={datetime.now(timezone.utc).isoformat()}")

    _load_env_files(args.env_file)
    user_hint, auth_mode = _prepare_kaggle_auth(
        kaggle_json=args.kaggle_json,
        owner_override=(args.owner or "").strip(),
    )
    _log(f"auth_mode={auth_mode}")

    data_root = Path(args.data_root)
    if not data_root.exists():
        alt = ROOT / "data"
        if alt.exists():
            _log(f"data-root missing, fallback {alt}")
            data_root = alt

    # 为拿到 username，需要先 authenticate（dry-run 也做，除非已提供 --owner）
    api = None
    if user_hint:
        username = user_hint
    else:
        api = _import_kaggle_api()
        username = _resolve_username(api, user_hint)
    dataset_id = f"{username}/{args.slug}"
    _log(f"target={dataset_id}")

    work = args.work_dir or (data_root / f"kaggle_upload_{stamp}")
    work = Path(work)
    work.parent.mkdir(parents=True, exist_ok=True)

    try:
        if args.upload_dir:
            src = Path(args.upload_dir)
            if not src.exists():
                _die(f"upload-dir 不存在: {src}")
            _log(f"copy upload-dir={src}")
            _stage_from_hf_upload(src, work)
        elif args.zip:
            zp = Path(args.zip)
            if not zp.exists():
                # 尝试 latest symlink
                latest = data_root / "hf_upload_latest.zip"
                if latest.exists():
                    zp = latest.resolve()
                    _log(f"zip missing, use {zp}")
                else:
                    _die(f"zip 不存在: {args.zip}")
            _stage_from_zip(zp, work)
        else:
            export_dir = _resolve_export_dir(args.export_dir, data_root)
            _log(f"export_dir={export_dir}")
            # 若同级有对应 zip 也可直接用
            maybe_zip = data_root / f"hf_upload_{export_dir.name.replace('public_dataset_', '')}.zip"
            if not maybe_zip.exists() and (data_root / "hf_upload_latest.zip").exists():
                maybe_zip = (data_root / "hf_upload_latest.zip").resolve()
            hf_dir = export_dir / "hf_upload"
            if hf_dir.exists():
                _stage_from_export_dir(export_dir, work)
            elif maybe_zip.exists():
                _log(f"hf_upload dir missing; unzip {maybe_zip}")
                _stage_from_zip(maybe_zip, work)
            else:
                _stage_from_export_dir(export_dir, work)

        staging_info = _preflight_staging(work)
        meta = _load_json(work / "dataset_meta.json")
        message = args.message.strip() or (
            f"export {meta.get('generated_at') or stamp} "
            f"players={meta.get('player_count')} stamp={stamp}"
        )
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        _write_dataset_metadata(
            work,
            dataset_id=dataset_id,
            title=args.title,
            subtitle=args.subtitle,
            license_name=args.license,
            keywords=keywords,
            meta=meta,
            staging_info=staging_info,
        )

        report = {
            "stamp": stamp,
            "dataset_id": dataset_id,
            "mode": args.mode,
            "public": bool(args.public),
            "message": message,
            "work_dir": str(work),
            "staging": staging_info,
            "dry_run": bool(args.dry_run),
        }

        if args.dry_run:
            _log("dry-run: metadata/preflight OK, skip network upload")
            url = f"https://www.kaggle.com/datasets/{dataset_id} (dry-run)"
        else:
            if api is None:
                api = _import_kaggle_api()
            url = _upload(
                api,
                work,
                mode=args.mode,
                public=args.public,
                message=message,
                dry_run=False,
            )
        report["url"] = url
        report["auth_mode"] = auth_mode
        # 报告里绝不写入 token/key
        report_path = data_root / f"kaggle_upload_report_{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        latest = data_root / "kaggle_upload_report_latest.json"
        try:
            latest.write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        _log(f"report={report_path}")
        _log(f"done url={url}")
        return 0
    finally:
        # 实际上传成功后默认清理暂存；dry-run / --keep-work 保留便于检查
        if (not args.keep_work) and (not args.dry_run) and work.exists():
            try:
                shutil.rmtree(work)
                _log(f"cleaned work_dir={work}")
            except Exception as e:
                _log(f"cleanup skipped: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
