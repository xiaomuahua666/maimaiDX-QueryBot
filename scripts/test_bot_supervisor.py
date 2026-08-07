#!/usr/bin/env python3
"""Static regression checks for the production Bot supervisor."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bot_supervisor.sh"
SOURCE = SCRIPT.read_text(encoding="utf-8")
SERVICE = (ROOT / "scripts" / "maimaidx-bot.service").read_text(encoding="utf-8")

subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

for required in (
    "git -C \"$REPO_DIR\" fetch --quiet origin main",
    "merge-base --is-ancestor HEAD origin/main",
    "merge --ff-only --quiet origin/main",
    "setsid \"$START_SCRIPT\" &",
    "collect_descendants \"$BOT_PID\"",
    "tree_alive \"${descendants[@]}\"",
    "signal_process_tree TERM",
    "signal_process_tree KILL",
    "graceful stop timed out",
    "trap shutdown INT TERM HUP",
):
    assert required in SOURCE, required

assert SOURCE.index("signal_process_tree TERM") < SOURCE.index("signal_process_tree KILL")
for required in (
    "After=network-online.target",
    "ExecStart=/www/bot/bot_supervisor.sh",
    "Restart=always",
    "KillMode=control-group",
    "WantedBy=multi-user.target",
):
    assert required in SERVICE, required

print("bot supervisor checks: OK")
