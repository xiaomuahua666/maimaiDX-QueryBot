#!/usr/bin/env bash
# 一键把最新（或指定）公开数据集上传到 Kaggle
#
# 用法：
#   bash scripts/run_upload_kaggle.sh
#   EXPORT_DIR=/www/bot/awmc/data/public_dataset_20260725_221219 bash scripts/run_upload_kaggle.sh
#   KAGGLE_PUBLIC=1 bash scripts/run_upload_kaggle.sh
#
# 凭证（写在 .env，不要提交 git / 不要写进命令行历史太久）：
#   # /www/bot/awmc/.env
#   KAGGLE_API_TOKEN=KGAT_xxxxxxxx
#   # 可选：KAGGLE_USERNAME=yourname
#
# 可选环境变量：
#   SLUG / TITLE / MODE(auto|create|version) / MESSAGE
#   EXPORT_DIR / HF_ZIP / DATA_ROOT
#   KAGGLE_PUBLIC=1  KAGGLE_DRY_RUN=1  KEEP_WORK=1
#   KAGGLE_ENV_FILE=/path/to/.env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STAMP="${STAMP:-$(date +%Y%m%d_%H%M%S)}"

DATA_ROOT="${DATA_ROOT:-/www/bot/awmc/data}"
if [[ ! -d "${DATA_ROOT}" ]]; then
  DATA_ROOT="${PLUGIN_DIR}/data"
fi

# 从本地 env 文件注入（不 echo 内容）
load_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  echo "[run] sourcing env file=${f}"
  set -a
  # shellcheck disable=SC1090
  source "$f"
  set +a
}
if [[ -n "${KAGGLE_ENV_FILE:-}" ]]; then
  load_env_file "${KAGGLE_ENV_FILE}"
fi
load_env_file "/www/bot/awmc/.env"
load_env_file "/www/bot/awmc/.env.kaggle"
load_env_file "${PLUGIN_DIR}/.env"

if [[ -n "${VENV_PYTHON:-}" ]]; then
  PYTHON="${VENV_PYTHON}"
elif [[ -x "/www/bot/.venv/bin/python" ]]; then
  PYTHON="/www/bot/.venv/bin/python"
elif [[ -x "${PLUGIN_DIR}/.venv/bin/python" ]]; then
  PYTHON="${PLUGIN_DIR}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

SLUG="${SLUG:-maimaidx-desensitized-scores}"
TITLE="${TITLE:-maimaiDX Desensitized Score Dataset}"
MODE="${MODE:-auto}"
MESSAGE="${MESSAGE:-}"
EXPORT_DIR="${EXPORT_DIR:-}"
HF_ZIP="${HF_ZIP:-}"
KAGGLE_PUBLIC="${KAGGLE_PUBLIC:-0}"
KAGGLE_DRY_RUN="${KAGGLE_DRY_RUN:-0}"
KEEP_WORK="${KEEP_WORK:-0}"
LOG_FILE="${LOG_FILE:-${DATA_ROOT}/kaggle_upload_${STAMP}.log}"

echo "[run] ===== kaggle upload preflight ====="
echo "[run] stamp=${STAMP}"
echo "[run] python=${PYTHON}"
echo "[run] data_root=${DATA_ROOT}"
echo "[run] slug=${SLUG}"
echo "[run] mode=${MODE}"
echo "[run] public=${KAGGLE_PUBLIC} dry_run=${KAGGLE_DRY_RUN}"
echo "[run] has_api_token=$([ -n "${KAGGLE_API_TOKEN:-}" ] && echo yes || echo no)"
echo "[run] has_legacy_key=$([ -n "${KAGGLE_KEY:-}" ] && echo yes || echo no)"
echo "[run] log=${LOG_FILE}"

if ! "${PYTHON}" -c "import kaggle" 2>/dev/null; then
  echo "[run] kaggle package missing; trying pip install ..."
  "${PYTHON}" -m pip install -q -U kaggle
fi

ARGS=(
  "${SCRIPT_DIR}/upload_public_dataset_to_kaggle.py"
  --data-root "${DATA_ROOT}"
  --slug "${SLUG}"
  --title "${TITLE}"
  --mode "${MODE}"
)

if [[ -n "${EXPORT_DIR}" ]]; then
  ARGS+=(--export-dir "${EXPORT_DIR}")
fi
if [[ -n "${HF_ZIP}" ]]; then
  ARGS+=(--zip "${HF_ZIP}")
fi
if [[ -n "${MESSAGE}" ]]; then
  ARGS+=(--message "${MESSAGE}")
fi
if [[ -n "${KAGGLE_ENV_FILE:-}" ]]; then
  ARGS+=(--env-file "${KAGGLE_ENV_FILE}")
fi
if [[ "${KAGGLE_PUBLIC}" == "1" ]]; then
  ARGS+=(--public)
fi
if [[ "${KAGGLE_DRY_RUN}" == "1" ]]; then
  ARGS+=(--dry-run --keep-work)
fi
if [[ "${KEEP_WORK}" == "1" ]]; then
  ARGS+=(--keep-work)
fi

mkdir -p "$(dirname "${LOG_FILE}")"
echo "[run] cmdline: ${PYTHON} ${ARGS[*]}"
set +e
"${PYTHON}" "${ARGS[@]}" 2>&1 | tee "${LOG_FILE}"
rc=${PIPESTATUS[0]}
set -e

echo "[run] exit=${rc}"
echo "[run] log=${LOG_FILE}"
if [[ -f "${DATA_ROOT}/kaggle_upload_report_latest.json" ]]; then
  echo "[run] report_latest=${DATA_ROOT}/kaggle_upload_report_latest.json"
  DATA_ROOT="${DATA_ROOT}" "${PYTHON}" - <<'PY'
import json
import os
from pathlib import Path
p = Path(os.environ["DATA_ROOT"]) / "kaggle_upload_report_latest.json"
print(json.dumps(json.loads(p.read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
PY
fi
exit "${rc}"
