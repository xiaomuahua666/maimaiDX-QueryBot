#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="${MAIMAIDX_REPO_DIR:-/www/bot/.venv/lib/python3.12/site-packages/nonebot_plugin_maimaidx}"
SUPERVISOR_PATTERN="${MAIMAIDX_SUPERVISOR_PATTERN:-^bash /www/bot/bot_supervisor\.sh$}"
BOT_PATTERN="${MAIMAIDX_BOT_PATTERN:-^/www/bot/.venv/bin/python3 /www/bot/.venv/bin/nb run$}"

first_pid() {
    pgrep -f "$1" 2>/dev/null | head -n 1 || true
}

supervisor_pid="$(first_pid "$SUPERVISOR_PATTERN")"
bot_pid="$(first_pid "$BOT_PATTERN")"
deployed_commit=""
uptime_seconds=0

if [[ -d "${REPO_DIR}/.git" ]]; then
    deployed_commit="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
fi

if [[ -n "$bot_pid" ]] && kill -0 "$bot_pid" 2>/dev/null; then
    state="running"
    uptime_seconds="$(ps -o etimes= -p "$bot_pid" 2>/dev/null | tr -d ' ' || true)"
    uptime_seconds="${uptime_seconds:-0}"
elif [[ -n "$supervisor_pid" ]] && kill -0 "$supervisor_pid" 2>/dev/null; then
    state="restarting"
    bot_pid=""
else
    state="stopped"
    bot_pid=""
fi

printf '{"state":"%s","supervisor_pid":%s,"bot_pid":%s,"uptime_seconds":%s,"deployed_commit":"%s"}\n' \
    "$state" \
    "${supervisor_pid:-null}" \
    "${bot_pid:-null}" \
    "$uptime_seconds" \
    "$deployed_commit"
