#!/usr/bin/env bash
# 服务器一键导出脱敏公开数据集（含质检 + HF 合规 zip）
#
# 用法（在服务器上）：
#   bash /www/bot/.venv/lib/python3.12/site-packages/nonebot_plugin_maimaidx/scripts/run_export_public_dataset.sh
#
# 可选环境变量：
#   SCORES_DIR / SHARE_CONFIG / OUTPUT_DIR / HF_ZIP / MUSIC_DATA / VENV_PYTHON
#   EXTRA_ARGS  追加传给 export_public_dataset.py 的参数
#   ENRICH=1            导出前从 player_cache/备份扩充（默认 1）
#   API_BACKFILL=0      对开启存储但仍无快照的用户调查分器补存（默认 0，较慢）
#   API_CONCURRENCY=3
#   API_LIMIT=0
#   STAMP=自定义时间戳   默认自动 YYYYMMDD_HHMMSS
#   KAGGLE_UPLOAD=0     导出成功后自动上传 Kaggle（需凭证；默认 0）
#   KAGGLE_PUBLIC=0     新建数据集时公开
#   KAGGLE_SLUG=dx-2026-awmcbot
#   WORKERS=12          phase1 并行扫描线程（默认 12；慢盘可降到 4）
#   CLEANUP=1           成功后清理旧导出/日志/中断产物（默认 1）
#   KEEP_N=1            保留最近 N 份完整导出（含对应 zip/log/enrich）
#   CLEANUP_INCOMPLETE=1  启动时先清中断的不完整目录/空 zip（默认 1）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STAMP="${STAMP:-$(date +%Y%m%d_%H%M%S)}"

# 生产默认路径；本地跑时若目录不存在会回退到插件 data/
DATA_ROOT="/www/bot/awmc/data"
if [[ ! -d "${DATA_ROOT}" ]]; then
  DATA_ROOT="${PLUGIN_DIR}/data"
fi

DEFAULT_SCORES="${PLUGIN_DIR}/data/user_scores"
DEFAULT_SHARE="${PLUGIN_DIR}/data/data_share_config.json"
DEFAULT_OUT="${DATA_ROOT}/public_dataset_${STAMP}"
DEFAULT_HF_ZIP="${DATA_ROOT}/hf_upload_${STAMP}.zip"
DEFAULT_LOG="${DATA_ROOT}/public_dataset_export_${STAMP}.log"
DEFAULT_ENRICH_REPORT="${DATA_ROOT}/enrich_report_${STAMP}.json"

SCORES_DIR="${SCORES_DIR:-$DEFAULT_SCORES}"
SHARE_CONFIG="${SHARE_CONFIG:-$DEFAULT_SHARE}"
OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUT}"
HF_ZIP="${HF_ZIP:-$DEFAULT_HF_ZIP}"
LOG_FILE="${LOG_FILE:-$DEFAULT_LOG}"
ENRICH_REPORT="${ENRICH_REPORT:-$DEFAULT_ENRICH_REPORT}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
ENRICH="${ENRICH:-1}"
API_BACKFILL="${API_BACKFILL:-0}"
API_CONCURRENCY="${API_CONCURRENCY:-3}"
API_LIMIT="${API_LIMIT:-0}"
KAGGLE_UPLOAD="${KAGGLE_UPLOAD:-0}"
KAGGLE_PUBLIC="${KAGGLE_PUBLIC:-0}"
KAGGLE_SLUG="${KAGGLE_SLUG:-dx-2026-awmcbot}"
WORKERS="${WORKERS:-12}"
CLEANUP="${CLEANUP:-1}"
KEEP_N="${KEEP_N:-1}"
CLEANUP_INCOMPLETE="${CLEANUP_INCOMPLETE:-1}"

# 自动探测曲库（改善 B15 划分）
MUSIC_DATA="${MUSIC_DATA:-}"
if [[ -z "${MUSIC_DATA}" ]]; then
  for cand in \
    "/www/bot/awmc/dxdata.json" \
    "${PLUGIN_DIR}/data/music_data.json" \
    "/www/bot/awmc/static/music_data.json" \
    "${PLUGIN_DIR}/../music_data.json"
  do
    if [[ -f "${cand}" ]]; then
      MUSIC_DATA="${cand}"
      break
    fi
  done
fi

if [[ -n "${VENV_PYTHON:-}" ]]; then
  PYTHON="${VENV_PYTHON}"
elif [[ -x "/www/bot/.venv/bin/python" ]]; then
  PYTHON="/www/bot/.venv/bin/python"
elif [[ -x "${PLUGIN_DIR}/.venv/bin/python" ]]; then
  PYTHON="${PLUGIN_DIR}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

mkdir -p "$(dirname "${OUTPUT_DIR}")" "$(dirname "${LOG_FILE}")" "$(dirname "${HF_ZIP}")" "$(dirname "${ENRICH_REPORT}")"

preflight() {
  echo "[run] ===== preflight checks ====="
  echo "[run] stamp=${STAMP}"
  echo "[run] time=$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "[run] host=$(hostname 2>/dev/null || echo unknown)"
  echo "[run] plugin=${PLUGIN_DIR}"
  echo "[run] python=${PYTHON}"
  echo "[run] scores=${SCORES_DIR}"
  echo "[run] share_config=${SHARE_CONFIG}"
  echo "[run] output=${OUTPUT_DIR}"
  echo "[run] hf_zip=${HF_ZIP}"
  echo "[run] enrich_report=${ENRICH_REPORT}"
  echo "[run] music_data=${MUSIC_DATA:-<none>}"
  echo "[run] enrich=${ENRICH} api_backfill=${API_BACKFILL}"
  echo "[run] workers=${WORKERS}"
  echo "[run] cleanup=${CLEANUP} keep_n=${KEEP_N} cleanup_incomplete=${CLEANUP_INCOMPLETE}"
  echo "[run] log=${LOG_FILE}"

  local fail=0
  if [[ ! -x "${PYTHON}" ]] && ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "[run] ERROR: python not found: ${PYTHON}"
    fail=1
  fi
  if [[ ! -d "${SCORES_DIR}" ]]; then
    echo "[run] ERROR: scores dir missing: ${SCORES_DIR}"
    fail=1
  else
    local n_dirs n_idx
    n_dirs=$(find "${SCORES_DIR}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
    n_idx=$(find "${SCORES_DIR}" -mindepth 2 -maxdepth 2 -name index.json 2>/dev/null | wc -l | tr -d ' ')
    echo "[run] scores_user_dirs=${n_dirs} with_index=${n_idx}"
  fi
  if [[ ! -f "${SHARE_CONFIG}" ]]; then
    echo "[run] WARN: share config missing (will treat as no opt-outs): ${SHARE_CONFIG}"
  else
    echo "[run] share_config_bytes=$(wc -c < "${SHARE_CONFIG}" | tr -d ' ')"
  fi
  if [[ -n "${MUSIC_DATA}" && ! -f "${MUSIC_DATA}" ]]; then
    echo "[run] WARN: music_data not found: ${MUSIC_DATA}"
  elif [[ -n "${MUSIC_DATA}" ]]; then
    echo "[run] music_data_bytes=$(wc -c < "${MUSIC_DATA}" | tr -d ' ')"
  fi
  if [[ ! -f "${SCRIPT_DIR}/export_public_dataset.py" ]]; then
    echo "[run] ERROR: export script missing"
    fail=1
  fi
  if [[ "${ENRICH}" == "1" && ! -f "${SCRIPT_DIR}/enrich_user_scores_for_dataset.py" ]]; then
    echo "[run] ERROR: enrich script missing"
    fail=1
  fi
  local cache_db="${PLUGIN_DIR}/data/player_cache/player_cache.db"
  if [[ -f "${cache_db}" ]]; then
    echo "[run] player_cache_db=${cache_db} bytes=$(wc -c < "${cache_db}" | tr -d ' ')"
  else
    echo "[run] WARN: live player_cache.db missing"
  fi
  if [[ "${fail}" -ne 0 ]]; then
    echo "[run] preflight FAILED"
    exit 1
  fi
  echo "[run] preflight OK"
}

link_latest() {
  # 在 data 根目录维护 latest 指针，便于下次找最新产物
  local root
  root="$(dirname "${OUTPUT_DIR}")"
  ln -sfn "$(basename "${OUTPUT_DIR}")" "${root}/public_dataset_latest" 2>/dev/null || true
  ln -sfn "$(basename "${HF_ZIP}")" "${root}/hf_upload_latest.zip" 2>/dev/null || true
  ln -sfn "$(basename "${LOG_FILE}")" "${root}/public_dataset_export_latest.log" 2>/dev/null || true
  ln -sfn "$(basename "${ENRICH_REPORT}")" "${root}/enrich_report_latest.json" 2>/dev/null || true
  echo "[run] latest symlinks updated under ${root}"
}

# 清理中断产物：无 dataset_meta 的目录、0 字节/残缺 zip、旧 launcher 输出
cleanup_incomplete_artifacts() {
  local root="$1"
  local removed=0
  echo "[cleanup] scan incomplete under ${root}"

  local d
  for d in "${root}"/public_dataset_*; do
    [[ -e "$d" ]] || continue
    [[ -L "$d" ]] && continue
    [[ -d "$d" ]] || continue
    if [[ ! -f "${d}/dataset_meta.json" ]]; then
      echo "[cleanup] rm incomplete dir: ${d}"
      rm -rf -- "${d}"
      removed=$((removed + 1))
    fi
  done

  # 未带时间戳的旧目录（早期导出）
  if [[ -d "${root}/public_dataset" && ! -f "${root}/public_dataset/dataset_meta.json" ]]; then
    echo "[cleanup] rm legacy incomplete: ${root}/public_dataset"
    rm -rf -- "${root}/public_dataset"
    removed=$((removed + 1))
  fi

  local z
  for z in "${root}"/hf_upload_*.zip "${root}"/public_dataset_share*.tar.gz "${root}"/hf_upload.zip; do
    [[ -e "$z" ]] || continue
    [[ -L "$z" ]] && continue
    if [[ ! -s "$z" ]]; then
      echo "[cleanup] rm empty archive: ${z}"
      rm -f -- "${z}"
      removed=$((removed + 1))
    fi
  done

  # 启动器残留
  local f
  for f in "${root}"/run_export_launcher_*.out "${root}"/public_dataset_export.nohup.out; do
    [[ -e "$f" ]] || continue
    echo "[cleanup] rm launcher out: ${f}"
    rm -f -- "${f}"
    removed=$((removed + 1))
  done

  echo "[cleanup] incomplete removed=${removed}"
}

# 成功导出后：只保留最近 KEEP_N 份完整导出及其配套 zip/log/enrich
cleanup_old_export_artifacts() {
  local root="$1"
  local keep_n="${2:-1}"
  local keep_current="${3:-}"
  keep_n="$(printf '%s' "${keep_n}" | tr -cd '0-9')"
  [[ -n "${keep_n}" ]] || keep_n=1
  (( keep_n < 1 )) && keep_n=1

  echo "[cleanup] rotate exports keep_n=${keep_n} current=${keep_current:-none}"

  local -a complete=()
  local d base stamp
  # 按时间戳名倒序（YYYYMMDD_HHMMSS）
  while IFS= read -r d; do
    [[ -n "$d" ]] || continue
    complete+=("$d")
  done < <(
    for d in "${root}"/public_dataset_*; do
      [[ -d "$d" && ! -L "$d" ]] || continue
      [[ -f "${d}/dataset_meta.json" ]] || continue
      echo "$(basename "$d")"
    done | sort -r
  )

  local -a keep_stamps=()
  if [[ -n "${keep_current}" ]]; then
    keep_stamps+=("${keep_current}")
  fi
  for base in "${complete[@]:-}"; do
    stamp="${base#public_dataset_}"
    already=0
    for x in "${keep_stamps[@]:-}"; do
      if [[ "${x}" == "${stamp}" ]]; then already=1; break; fi
    done
    (( already )) && continue
    if (( ${#keep_stamps[@]} < keep_n )); then
      keep_stamps+=("${stamp}")
    fi
  done

  _should_keep_stamp() {
    local s="$1" x
    for x in "${keep_stamps[@]:-}"; do
      [[ "${x}" == "${s}" ]] && return 0
    done
    return 1
  }

  echo "[cleanup] keep stamps: ${keep_stamps[*]:-<none>}"

  for d in "${root}"/public_dataset_*; do
    [[ -d "$d" && ! -L "$d" ]] || continue
    base="$(basename "$d")"
    stamp="${base#public_dataset_}"
    if _should_keep_stamp "${stamp}"; then
      continue
    fi
    echo "[cleanup] rm old dataset: ${d}"
    rm -rf -- "${d}"
  done

  # 旧无时间戳目录（完整也删，已有 stamped 版本）
  if [[ -d "${root}/public_dataset" && ! -L "${root}/public_dataset" ]]; then
    echo "[cleanup] rm legacy dataset dir: ${root}/public_dataset"
    rm -rf -- "${root}/public_dataset"
  fi

  for z in "${root}"/hf_upload_*.zip; do
    [[ -e "$z" && ! -L "$z" ]] || continue
    base="$(basename "$z")"
    stamp="${base#hf_upload_}"
    stamp="${stamp%.zip}"
    if _should_keep_stamp "${stamp}"; then
      continue
    fi
    echo "[cleanup] rm old zip: ${z}"
    rm -f -- "${z}"
  done
  for z in "${root}"/public_dataset_share*.tar.gz "${root}"/hf_upload.zip; do
    [[ -e "$z" && ! -L "$z" ]] || continue
    echo "[cleanup] rm share/legacy archive: ${z}"
    rm -f -- "${z}"
  done

  for f in \
    "${root}"/public_dataset_export_*.log \
    "${root}"/enrich_report_*.json \
    "${root}"/run_export_launcher_*.out \
    "${root}"/public_dataset_export.log \
    "${root}"/public_dataset_export.nohup.out
  do
    [[ -e "$f" && ! -L "$f" ]] || continue
    base="$(basename "$f")"
    stamp="__drop__"
    case "${base}" in
      public_dataset_export_*.log)
        stamp="${base#public_dataset_export_}"; stamp="${stamp%.log}" ;;
      enrich_report_*.json)
        stamp="${base#enrich_report_}"; stamp="${stamp%.json}" ;;
      run_export_launcher_*.out)
        stamp="${base#run_export_launcher_}"; stamp="${stamp%.out}" ;;
    esac
    if [[ "${stamp}" != "__drop__" ]] && _should_keep_stamp "${stamp}"; then
      continue
    fi
    echo "[cleanup] rm old log/report: ${f}"
    rm -f -- "${f}"
  done

  # 刷新 latest 软链（指向仍存在的当前产物）
  if [[ -n "${keep_current}" ]]; then
    [[ -d "${root}/public_dataset_${keep_current}" ]] && \
      ln -sfn "public_dataset_${keep_current}" "${root}/public_dataset_latest" || true
    [[ -f "${root}/hf_upload_${keep_current}.zip" ]] && \
      ln -sfn "hf_upload_${keep_current}.zip" "${root}/hf_upload_latest.zip" || true
    [[ -f "${root}/public_dataset_export_${keep_current}.log" ]] && \
      ln -sfn "public_dataset_export_${keep_current}.log" "${root}/public_dataset_export_latest.log" || true
    [[ -f "${root}/enrich_report_${keep_current}.json" ]] && \
      ln -sfn "enrich_report_${keep_current}.json" "${root}/enrich_report_latest.json" || true
  fi

  echo "[cleanup] export rotate done; disk:"
  du -sh "${root}" 2>/dev/null || true
}

# 仅清理：CLEANUP_ONLY=1 bash .../run_export_public_dataset.sh
# 或：bash .../run_export_public_dataset.sh --cleanup-only
if [[ "${1:-}" == "--cleanup-only" || "${CLEANUP_ONLY:-0}" == "1" ]]; then
  echo "[run] cleanup-only mode data_root=${DATA_ROOT} keep_n=${KEEP_N}"
  cleanup_incomplete_artifacts "${DATA_ROOT}"
  latest_stamp=""
  while IFS= read -r base; do
    latest_stamp="${base#public_dataset_}"
    break
  done < <(
    for d in "${DATA_ROOT}"/public_dataset_*; do
      [[ -d "$d" && ! -L "$d" && -f "${d}/dataset_meta.json" ]] || continue
      echo "$(basename "$d")"
    done | sort -r
  )
  cleanup_old_export_artifacts "${DATA_ROOT}" "${KEEP_N}" "${latest_stamp}"
  # kaggle 侧旧日志
  CLEANUP_ONLY=1 KEEP_N="${KEEP_N}" CLEANUP_UPLOADED_ZIP=0 DATA_ROOT="${DATA_ROOT}" \
    bash "${SCRIPT_DIR}/run_upload_kaggle.sh" --cleanup-only || true
  echo "[run] cleanup-only done"
  du -sh "${DATA_ROOT}" 2>/dev/null || true
  ls -lah "${DATA_ROOT}" | head -60 || true
  exit 0
fi

preflight

cd "${PLUGIN_DIR}"

if [[ "${CLEANUP_INCOMPLETE}" == "1" ]]; then
  cleanup_incomplete_artifacts "${DATA_ROOT}"
fi

{
  echo "[run] ===== job start stamp=${STAMP} ====="
  if [[ "${ENRICH}" == "1" ]]; then
    echo "[run] === enrich user_scores from cache/backups ==="
    ENRICH_ARGS=(
      "${SCRIPT_DIR}/enrich_user_scores_for_dataset.py"
      --scores-dir "${SCORES_DIR}"
      --share-config "${SHARE_CONFIG}"
      --min-records 30
      --report "${ENRICH_REPORT}"
    )
    if [[ "${API_BACKFILL}" == "1" ]]; then
      ENRICH_ARGS+=(--api-backfill --concurrency "${API_CONCURRENCY}")
      if [[ "${API_LIMIT}" != "0" ]]; then
        ENRICH_ARGS+=(--api-limit "${API_LIMIT}")
      fi
    fi
    "${PYTHON}" "${ENRICH_ARGS[@]}"
  else
    echo "[run] enrich skipped (ENRICH=${ENRICH})"
  fi

  echo "[run] === export public dataset ==="
  ARGS=(
    "${SCRIPT_DIR}/export_public_dataset.py"
    --scores-dir "${SCORES_DIR}"
    --share-config "${SHARE_CONFIG}"
    --output "${OUTPUT_DIR}"
    --hf-zip
    --hf-zip-path "${HF_ZIP}"
    --include-full-records
    --bucket-size 200
    --min-bucket-players 8
    --min-chart-samples 3
    --trend-days 90
    --min-records 30
    --min-rating 1000
    --min-b50 20
    --workers "${WORKERS}"
  )

  if [[ -n "${MUSIC_DATA}" ]]; then
    ARGS+=(--music-data "${MUSIC_DATA}")
  fi

  if [[ -n "${EXTRA_ARGS}" ]]; then
    read -r -a EXTRA_ARR <<< "${EXTRA_ARGS}"
    ARGS+=("${EXTRA_ARR[@]}")
  fi

  echo "[run] export cmdline: ${PYTHON} ${ARGS[*]}"
  "${PYTHON}" "${ARGS[@]}"
  echo "[run] ===== job finished stamp=${STAMP} ====="
} 2>&1 | tee "${LOG_FILE}"
rc=${PIPESTATUS[0]}

echo
echo "[run] exit=${rc}"
echo "[run] stamp=${STAMP}"
echo "[run] output=${OUTPUT_DIR}"
echo "[run] meta=${OUTPUT_DIR}/dataset_meta.json"
echo "[run] quality=${OUTPUT_DIR}/quality_report.json"
echo "[run] enrich_report=${ENRICH_REPORT}"
echo "[run] hf_zip=${HF_ZIP}"
echo "[run] log=${LOG_FILE}"

if [[ "${rc}" -eq 0 ]]; then
  link_latest
  if [[ -f "${OUTPUT_DIR}/dataset_meta.json" ]]; then
    echo "[run] dataset_meta:"
    OUTPUT_DIR="${OUTPUT_DIR}" "${PYTHON}" - <<'PY'
import json
import os
from pathlib import Path
p = Path(os.environ["OUTPUT_DIR"]) / "dataset_meta.json"
print(json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
PY
  fi

  if [[ "${CLEANUP}" == "1" ]]; then
    cleanup_old_export_artifacts "$(dirname "${OUTPUT_DIR}")" "${KEEP_N}" "${STAMP}"
  fi

  if [[ "${KAGGLE_UPLOAD}" == "1" ]]; then
    echo "[run] === upload to Kaggle ==="
    export DATA_ROOT="$(dirname "${OUTPUT_DIR}")"
    export EXPORT_DIR="${OUTPUT_DIR}"
    export HF_ZIP="${HF_ZIP}"
    export SLUG="${KAGGLE_SLUG}"
    export MODE="${KAGGLE_MODE:-auto}"
    export KAGGLE_PUBLIC
    export STAMP
    export CLEANUP
    export KEEP_N
    bash "${SCRIPT_DIR}/run_upload_kaggle.sh" || {
      echo "[run] WARN: Kaggle upload failed (export itself succeeded)"
      rc=2
    }
  fi
fi

exit "${rc}"
