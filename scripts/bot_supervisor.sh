#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="${MAIMAIDX_REPO_DIR:-/www/bot/.venv/lib/python3.12/site-packages/nonebot_plugin_maimaidx}"
START_SCRIPT="${MAIMAIDX_START_SCRIPT:-${REPO_DIR}/scripts/start_bot_32core.sh}"
UPDATE_INTERVAL="${MAIMAIDX_UPDATE_INTERVAL:-60}"
RESTART_DELAY="${MAIMAIDX_RESTART_DELAY:-5}"
STOP_TIMEOUT="${MAIMAIDX_STOP_TIMEOUT:-20}"

BOT_PID=""
STOPPING=0

log() {
    printf '%s [bot-supervisor] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

process_alive() {
    local pid="$1"
    local state
    kill -0 "$pid" 2>/dev/null || return 1
    state="$(ps -o stat= -p "$pid" 2>/dev/null || true)"
    [[ -n "$state" && "$state" != Z* ]]
}

collect_descendants() {
    local parent="$1"
    local child
    while read -r child; do
        [[ -n "$child" ]] || continue
        collect_descendants "$child"
        printf '%s\n' "$child"
    done < <(pgrep -P "$parent" 2>/dev/null || true)
}

signal_process_tree() {
    local signal="$1"
    shift
    local pid

    if [[ -n "$BOT_PID" ]]; then
        kill "-${signal}" -- "-${BOT_PID}" 2>/dev/null || true
    fi
    for pid in "$@"; do
        kill "-${signal}" "$pid" 2>/dev/null || true
    done
}

tree_alive() {
    local pid
    process_alive "$BOT_PID" && return 0
    for pid in "$@"; do
        process_alive "$pid" && return 0
    done
    return 1
}

stop_bot() {
    [[ -n "$BOT_PID" ]] || return 0

    local deadline=$((SECONDS + STOP_TIMEOUT))
    local descendants=()
    mapfile -t descendants < <(collect_descendants "$BOT_PID")
    log "stopping bot pid=${BOT_PID}, descendants=${#descendants[@]}"
    signal_process_tree TERM "${descendants[@]}"

    while tree_alive "${descendants[@]}" && (( SECONDS < deadline )); do
        sleep 1
    done

    if tree_alive "${descendants[@]}"; then
        local current_descendants=()
        mapfile -t current_descendants < <(collect_descendants "$BOT_PID")
        descendants+=("${current_descendants[@]}")
        log "graceful stop timed out after ${STOP_TIMEOUT}s; forcing SIGKILL"
        signal_process_tree KILL "${descendants[@]}"
    fi

    wait "$BOT_PID" 2>/dev/null || true
    BOT_PID=""
}

shutdown() {
    STOPPING=1
    log "supervisor shutdown requested"
    stop_bot
    exit 0
}

start_bot() {
    if [[ ! -x "$START_SCRIPT" ]]; then
        log "start script is missing or not executable: ${START_SCRIPT}"
        return 1
    fi
    log "starting bot from ${START_SCRIPT}"
    setsid "$START_SCRIPT" &
    BOT_PID=$!
    log "bot started pid=${BOT_PID}"
}

update_available() {
    local local_head remote_head

    if ! git -C "$REPO_DIR" fetch --quiet origin main; then
        log "git fetch failed; keeping the current bot running"
        return 1
    fi
    local_head="$(git -C "$REPO_DIR" rev-parse HEAD)" || return 1
    remote_head="$(git -C "$REPO_DIR" rev-parse origin/main)" || return 1
    [[ "$local_head" != "$remote_head" ]]
}

apply_update() {
    if ! git -C "$REPO_DIR" diff --quiet || ! git -C "$REPO_DIR" diff --cached --quiet; then
        log "tracked production files are modified; refusing automatic update"
        return 1
    fi
    if ! git -C "$REPO_DIR" merge-base --is-ancestor HEAD origin/main; then
        log "origin/main is not a fast-forward update; manual intervention required"
        return 1
    fi
    if ! git -C "$REPO_DIR" merge --ff-only --quiet origin/main; then
        log "fast-forward update failed; keeping the current bot running"
        return 1
    fi
    log "updated to $(git -C "$REPO_DIR" rev-parse --short HEAD)"
}

trap shutdown INT TERM HUP

if [[ ! -d "${REPO_DIR}/.git" ]]; then
    log "repository not found: ${REPO_DIR}"
    exit 1
fi

while (( ! STOPPING )); do
    if ! start_bot; then
        sleep "$RESTART_DELAY"
        continue
    fi

    while process_alive "$BOT_PID"; do
        sleep "$UPDATE_INTERVAL" &
        wait $! || true
        (( STOPPING )) && break
        if update_available && apply_update; then
            stop_bot
            break
        fi
    done

    if [[ -n "$BOT_PID" ]]; then
        wait "$BOT_PID" 2>/dev/null || true
        log "bot exited; restarting in ${RESTART_DELAY}s"
        BOT_PID=""
    fi
    (( STOPPING )) || sleep "$RESTART_DELAY"
done
