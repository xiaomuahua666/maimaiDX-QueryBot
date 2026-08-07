#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="${MAIMAIDX_REPO_DIR:-/www/bot/.venv/lib/python3.12/site-packages/nonebot_plugin_maimaidx}"
SUPERVISOR_PATTERN="${MAIMAIDX_SUPERVISOR_PATTERN:-^bash /www/bot/bot_supervisor\.sh$}"
BOT_PATTERN="${MAIMAIDX_BOT_PATTERN:-^/www/bot/.venv/bin/python3 /www/bot/.venv/bin/nb run$}"

first_pid() {
    pgrep -f "$1" 2>/dev/null | head -n 1 || true
}

process_alive() {
    local pid="$1" state
    [[ -n "$pid" ]] || return 1
    state="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')"
    [[ -n "$state" && "$state" != Z* ]]
}

supervisor_pid="$(first_pid "$SUPERVISOR_PATTERN")"
bot_pid="$(first_pid "$BOT_PATTERN")"
deployed_commit=""
deployed_at_epoch=0
uptime_seconds=0
bot_started_at_epoch=0

if [[ -d "${REPO_DIR}/.git" ]]; then
    deployed_commit="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
    head_ref="$(git -C "$REPO_DIR" symbolic-ref -q HEAD 2>/dev/null || true)"
    if [[ -n "$head_ref" && -f "${REPO_DIR}/.git/${head_ref}" ]]; then
        deployed_at_epoch="$(stat -c %Y "${REPO_DIR}/.git/${head_ref}" 2>/dev/null || true)"
        deployed_at_epoch="${deployed_at_epoch:-0}"
    fi
fi

if process_alive "$bot_pid"; then
    state="running"
    uptime_seconds="$(ps -o etimes= -p "$bot_pid" 2>/dev/null | tr -d ' ' || true)"
    uptime_seconds="${uptime_seconds:-0}"
    bot_started_at_epoch="$(($(date +%s) - uptime_seconds))"
    if (( deployed_at_epoch > bot_started_at_epoch )); then
        state="updating"
    fi
elif process_alive "$supervisor_pid"; then
    state="restarting"
    bot_pid=""
else
    state="stopped"
    bot_pid=""
fi

printf '{"state":"%s","supervisor_pid":%s,"bot_pid":%s,"uptime_seconds":%s,"bot_started_at_epoch":%s,"deployed_commit":"%s","deployed_at_epoch":%s}\n' \
    "$state" \
    "${supervisor_pid:-null}" \
    "${bot_pid:-null}" \
    "$uptime_seconds" \
    "$bot_started_at_epoch" \
    "$deployed_commit" \
    "$deployed_at_epoch"
