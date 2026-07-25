#!/usr/bin/env bash
# 服务器一键导出脱敏公开数据集（含质检 + HF 合规 zip）
#
# 用法（在服务器上）：
#   bash /www/bot/.venv/lib/python3.12/site-packages/nonebot_plugin_maimaidx/scripts/run_export_public_dataset.sh
#   # 或仓库内：
#   bash scripts/run_export_public_dataset.sh
#
# 可选环境变量：
#   SCORES_DIR / SHARE_CONFIG / OUTPUT_DIR / HF_ZIP / MUSIC_DATA / VENV_PYTHON
#   EXTRA_ARGS  追加传给 export_public_dataset.py 的参数
#   ENRICH=1            导出前从 player_cache/备份扩充（默认 1）
#   API_BACKFILL=0      对开启存储但仍无快照的用户调查分器补存（默认 0，较慢）
#   API_CONCURRENCY=3
#   API_LIMIT=0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 生产默认路径；本地跑时若目录不存在会回退到插件 data/
DEFAULT_SCORES="${PLUGIN_DIR}/data/user_scores"
DEFAULT_SHARE="${PLUGIN_DIR}/data/data_share_config.json"
DEFAULT_OUT="/www/bot/awmc/data/public_dataset"
DEFAULT_HF_ZIP="/www/bot/awmc/data/hf_upload.zip"
DEFAULT_LOG="/www/bot/awmc/data/public_dataset_export.log"

if [[ ! -d "/www/bot/awmc/data" ]]; then
  DEFAULT_OUT="${PLUGIN_DIR}/data/public_dataset"
  DEFAULT_HF_ZIP="${PLUGIN_DIR}/data/hf_upload.zip"
  DEFAULT_LOG="${PLUGIN_DIR}/data/public_dataset_export.log"
fi

SCORES_DIR="${SCORES_DIR:-$DEFAULT_SCORES}"
SHARE_CONFIG="${SHARE_CONFIG:-$DEFAULT_SHARE}"
OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUT}"
HF_ZIP="${HF_ZIP:-$DEFAULT_HF_ZIP}"
LOG_FILE="${LOG_FILE:-$DEFAULT_LOG}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
ENRICH="${ENRICH:-1}"
API_BACKFILL="${API_BACKFILL:-0}"
API_CONCURRENCY="${API_CONCURRENCY:-3}"
API_LIMIT="${API_LIMIT:-0}"

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

mkdir -p "$(dirname "${OUTPUT_DIR}")" "$(dirname "${LOG_FILE}")" "$(dirname "${HF_ZIP}")"

echo "[run] plugin=${PLUGIN_DIR}"
echo "[run] python=${PYTHON}"
echo "[run] scores=${SCORES_DIR}"
echo "[run] output=${OUTPUT_DIR}"
echo "[run] hf_zip=${HF_ZIP}"
echo "[run] music_data=${MUSIC_DATA:-<none>}"
echo "[run] enrich=${ENRICH} api_backfill=${API_BACKFILL}"
echo "[run] log=${LOG_FILE}"

cd "${PLUGIN_DIR}"

{
  if [[ "${ENRICH}" == "1" ]]; then
    echo "[run] === enrich user_scores from cache/backups ==="
    ENRICH_ARGS=(
      "${SCRIPT_DIR}/enrich_user_scores_for_dataset.py"
      --scores-dir "${SCORES_DIR}"
      --share-config "${SHARE_CONFIG}"
      --min-records 30
    )
    if [[ "${API_BACKFILL}" == "1" ]]; then
      ENRICH_ARGS+=(--api-backfill --concurrency "${API_CONCURRENCY}")
      if [[ "${API_LIMIT}" != "0" ]]; then
        ENRICH_ARGS+=(--api-limit "${API_LIMIT}")
      fi
    fi
    "${PYTHON}" "${ENRICH_ARGS[@]}"
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
  )

  if [[ -n "${MUSIC_DATA}" ]]; then
    ARGS+=(--music-data "${MUSIC_DATA}")
  fi

  if [[ -n "${EXTRA_ARGS}" ]]; then
    # 允许 EXTRA_ARGS='--require-trend --min-trend-points 2'
    read -r -a EXTRA_ARR <<< "${EXTRA_ARGS}"
    ARGS+=("${EXTRA_ARR[@]}")
  fi

  "${PYTHON}" "${ARGS[@]}"
} 2>&1 | tee "${LOG_FILE}"
rc=${PIPESTATUS[0]}

echo
echo "[run] exit=${rc}"
echo "[run] meta=${OUTPUT_DIR}/dataset_meta.json"
echo "[run] quality=${OUTPUT_DIR}/quality_report.json"
echo "[run] enrich_report=${SCORES_DIR%/*}/enrich_report.json"
echo "[run] hf_zip=${HF_ZIP}"
echo "[run] log=${LOG_FILE}"
exit "${rc}"
