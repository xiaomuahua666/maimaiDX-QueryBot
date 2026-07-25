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
#   KEYWORDS=games         # Kaggle tags，须小写官方名（games / video games）
#   EXPORT_DIR / HF_ZIP / DATA_ROOT
#   KAGGLE_PUBLIC=1  KAGGLE_DRY_RUN=1  KEEP_WORK=1
#   KAGGLE_ENV_FILE=/path/to/.env
#   默认 version notes：Version N - YYYYMMDDHHMMSS
#   CLEANUP=1              上传成功后清理旧 kaggle 日志/报告（默认 1）
#   KEEP_N=1               保留最近 N 份 kaggle 日志/报告
#   CLEANUP_UPLOADED_ZIP=1 上传成功后删除本次已上传的本地 zip（默认 1，省空间）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STAMP="${STAMP:-$(date +%Y%m%d_%H%M%S)}"

DATA_ROOT="${DATA_ROOT:-/www/bot/awmc/data}"
if [[ ! -d "${DATA_ROOT}" ]]; then
  DATA_ROOT="${PLUGIN_DIR}/data"
fi

# 安全读取 KEY=VALUE（绝不 source Bot 的 .env：里面常有未加引号的特殊字符）
# 只导出 KAGGLE_*；完整加载仍由 upload_public_dataset_to_kaggle.py 负责。
load_kaggle_env_keys() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  echo "[run] load kaggle keys from=${f}"
  local line key val
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    [[ "${line}" == export\ * ]] && line="${line#export }"
    [[ "${line}" == KAGGLE_* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "${val}" == \"*\" && "${val}" == *\" ]]; then
      val="${val:1:${#val}-2}"
    elif [[ "${val}" == \'*\' && "${val}" == *\' ]]; then
      val="${val:1:${#val}-2}"
    fi
    # 已在外部环境显式设置的不覆盖
    if [[ -n "${!key:-}" ]]; then
      continue
    fi
    export "${key}=${val}"
  done < "${f}"
}
if [[ -n "${KAGGLE_ENV_FILE:-}" ]]; then
  load_kaggle_env_keys "${KAGGLE_ENV_FILE}"
fi
# 优先专用文件，再扫 Bot .env（只取 KAGGLE_*）
load_kaggle_env_keys "/www/bot/awmc/.env.kaggle"
load_kaggle_env_keys "/www/bot/awmc/.env"
load_kaggle_env_keys "${PLUGIN_DIR}/.env"

if [[ -n "${VENV_PYTHON:-}" ]]; then
  PYTHON="${VENV_PYTHON}"
elif [[ -x "/www/bot/.venv/bin/python" ]]; then
  PYTHON="/www/bot/.venv/bin/python"
elif [[ -x "${PLUGIN_DIR}/.venv/bin/python" ]]; then
  PYTHON="${PLUGIN_DIR}/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi

# 默认更新已有数据集 https://www.kaggle.com/datasets/awmcteam/dx-2026-awmcbot
SLUG="${SLUG:-dx-2026-awmcbot}"
TITLE="${TITLE:-舞萌DX 2026 AWMCBOT 用户成绩数据集}"
MODE="${MODE:-version}"
MESSAGE="${MESSAGE:-}"
KEYWORDS="${KEYWORDS:-games}"
EXPORT_DIR="${EXPORT_DIR:-}"
HF_ZIP="${HF_ZIP:-}"
KAGGLE_PUBLIC="${KAGGLE_PUBLIC:-0}"
KAGGLE_DRY_RUN="${KAGGLE_DRY_RUN:-0}"
KEEP_WORK="${KEEP_WORK:-0}"
LOG_FILE="${LOG_FILE:-${DATA_ROOT}/kaggle_upload_${STAMP}.log}"
CLEANUP="${CLEANUP:-1}"
KEEP_N="${KEEP_N:-1}"
CLEANUP_UPLOADED_ZIP="${CLEANUP_UPLOADED_ZIP:-1}"

cleanup_kaggle_artifacts() {
  local root="$1"
  local keep_n="${2:-1}"
  local keep_stamp="${3:-}"
  local uploaded_zip="${4:-}"
  keep_n="$(printf '%s' "${keep_n}" | tr -cd '0-9')"
  [[ -n "${keep_n}" ]] || keep_n=1
  (( keep_n < 1 )) && keep_n=1

  echo "[cleanup] kaggle logs keep_n=${keep_n} current=${keep_stamp:-none}"

  local -a stamps=()
  local f base stamp
  while IFS= read -r stamp; do
    [[ -n "${stamp}" ]] || continue
    stamps+=("${stamp}")
  done < <(
    for f in "${root}"/kaggle_upload_*.log; do
      [[ -f "$f" && ! -L "$f" ]] || continue
      base="$(basename "$f")"
      stamp="${base#kaggle_upload_}"
      stamp="${stamp%.log}"
      echo "${stamp}"
    done | sort -r
  )

  local -a keep_stamps=()
  if [[ -n "${keep_stamp}" ]]; then
    keep_stamps+=("${keep_stamp}")
  fi
  local already x
  for stamp in "${stamps[@]:-}"; do
    already=0
    for x in "${keep_stamps[@]:-}"; do
      [[ "${x}" == "${stamp}" ]] && already=1 && break
    done
    (( already )) && continue
    if (( ${#keep_stamps[@]} < keep_n )); then
      keep_stamps+=("${stamp}")
    fi
  done

  _keep() {
    local s="$1" x
    for x in "${keep_stamps[@]:-}"; do
      [[ "${x}" == "${s}" ]] && return 0
    done
    return 1
  }

  echo "[cleanup] keep kaggle stamps: ${keep_stamps[*]:-<none>}"

  for f in "${root}"/kaggle_upload_*.log "${root}"/kaggle_upload_report_*.json "${root}"/run_kaggle_launcher_*.out; do
    [[ -e "$f" && ! -L "$f" ]] || continue
    base="$(basename "$f")"
    stamp="__drop__"
    case "${base}" in
      kaggle_upload_report_latest.json) continue ;;
      kaggle_upload_*.log)
        stamp="${base#kaggle_upload_}"; stamp="${stamp%.log}" ;;
      kaggle_upload_report_*.json)
        stamp="${base#kaggle_upload_report_}"; stamp="${stamp%.json}" ;;
      run_kaggle_launcher_*.out)
        stamp="${base#run_kaggle_launcher_}"; stamp="${stamp%.out}" ;;
    esac
    if [[ "${stamp}" != "__drop__" ]] && _keep "${stamp}"; then
      continue
    fi
    echo "[cleanup] rm old kaggle file: ${f}"
    rm -f -- "${f}"
  done

  if [[ "${CLEANUP_UPLOADED_ZIP}" == "1" && -n "${uploaded_zip}" && -f "${uploaded_zip}" && ! -L "${uploaded_zip}" ]]; then
    echo "[cleanup] rm uploaded zip (already on Kaggle): ${uploaded_zip}"
    rm -f -- "${uploaded_zip}"
    # 若 latest 指向它，删掉坏链
    if [[ -L "${root}/hf_upload_latest.zip" ]]; then
      local tgt
      tgt="$(readlink "${root}/hf_upload_latest.zip" || true)"
      if [[ "$(basename "${uploaded_zip}")" == "${tgt}" ]]; then
        rm -f -- "${root}/hf_upload_latest.zip"
        echo "[cleanup] removed stale hf_upload_latest.zip symlink"
      fi
    fi
  fi

  # 刷新 report latest（若当前报告还在）
  if [[ -n "${keep_stamp}" && -f "${root}/kaggle_upload_report_${keep_stamp}.json" ]]; then
    ln -sfn "kaggle_upload_report_${keep_stamp}.json" "${root}/kaggle_upload_report_latest.json" 2>/dev/null || true
  fi

  echo "[cleanup] kaggle cleanup done; disk:"
  du -sh "${root}" 2>/dev/null || true
}

# 仅清理日志：bash scripts/run_upload_kaggle.sh --cleanup-only
if [[ "${1:-}" == "--cleanup-only" || "${CLEANUP_ONLY:-0}" == "1" ]]; then
  echo "[run] kaggle cleanup-only data_root=${DATA_ROOT} keep_n=${KEEP_N}"
  latest_stamp=""
  while IFS= read -r s; do
    latest_stamp="$s"
    break
  done < <(
    for f in "${DATA_ROOT}"/kaggle_upload_*.log; do
      [[ -f "$f" && ! -L "$f" ]] || continue
      b="$(basename "$f")"
      echo "${b#kaggle_upload_}" | sed 's/\.log$//'
    done | sort -r
  )
  cleanup_kaggle_artifacts "${DATA_ROOT}" "${KEEP_N}" "${latest_stamp}" ""
  exit 0
fi

echo "[run] ===== kaggle upload preflight ====="
echo "[run] stamp=${STAMP}"
echo "[run] python=${PYTHON}"
echo "[run] data_root=${DATA_ROOT}"
echo "[run] slug=${SLUG}"
echo "[run] mode=${MODE}"
echo "[run] keywords=${KEYWORDS}"
echo "[run] public=${KAGGLE_PUBLIC} dry_run=${KAGGLE_DRY_RUN}"
echo "[run] cleanup=${CLEANUP} keep_n=${KEEP_N} cleanup_uploaded_zip=${CLEANUP_UPLOADED_ZIP}"
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
  --keywords "${KEYWORDS}"
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

if [[ "${rc}" -eq 0 && "${CLEANUP}" == "1" && "${KAGGLE_DRY_RUN}" != "1" ]]; then
  # 解析实际上传的 zip（优先显式 HF_ZIP，否则从 latest 报告/软链猜）
  UPLOADED_ZIP="${HF_ZIP:-}"
  if [[ -z "${UPLOADED_ZIP}" && -L "${DATA_ROOT}/hf_upload_latest.zip" ]]; then
    UPLOADED_ZIP="${DATA_ROOT}/$(readlink "${DATA_ROOT}/hf_upload_latest.zip")"
  fi
  if [[ -z "${UPLOADED_ZIP}" && -f "${DATA_ROOT}/kaggle_upload_report_latest.json" ]]; then
    UPLOADED_ZIP="$(
      DATA_ROOT="${DATA_ROOT}" "${PYTHON}" - <<'PY'
import json
import os
from pathlib import Path
p = Path(os.environ["DATA_ROOT"]) / "kaggle_upload_report_latest.json"
try:
    d = json.loads(p.read_text(encoding="utf-8"))
except Exception:
    d = {}
print(d.get("zip") or d.get("hf_zip") or d.get("zip_path") or "")
PY
    )"
  fi
  cleanup_kaggle_artifacts "${DATA_ROOT}" "${KEEP_N}" "${STAMP}" "${UPLOADED_ZIP}"
fi

exit "${rc}"
