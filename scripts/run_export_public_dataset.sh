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

preflight

cd "${PLUGIN_DIR}"

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

  if [[ "${KAGGLE_UPLOAD}" == "1" ]]; then
    echo "[run] === upload to Kaggle ==="
    export DATA_ROOT="$(dirname "${OUTPUT_DIR}")"
    export EXPORT_DIR="${OUTPUT_DIR}"
    export HF_ZIP="${HF_ZIP}"
    export SLUG="${KAGGLE_SLUG}"
    export MODE="${KAGGLE_MODE:-auto}"
    export KAGGLE_PUBLIC
    export STAMP
    bash "${SCRIPT_DIR}/run_upload_kaggle.sh" || {
      echo "[run] WARN: Kaggle upload failed (export itself succeeded)"
      rc=2
    }
  fi
fi

exit "${rc}"
