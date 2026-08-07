#!/usr/bin/env python3
"""Regression checks for the Feishu main-push card."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "feishu_push_notify", ROOT / "scripts" / "feishu_push_notify.py"
)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SHA = "0123456789abcdef0123456789abcdef01234567"
EVENT = {
    "after": SHA,
    "compare": "https://github.com/AWMC-TEAM/maimaiDX-QueryBot/compare/a...b",
    "commits": [{"id": SHA}, {"id": "b" * 40}],
    "head_commit": {
        "message": "fix: verify Feishu card\n\nLong body",
        "url": f"https://github.com/AWMC-TEAM/maimaiDX-QueryBot/commit/{SHA}",
        "author": {"name": "Milk", "username": "Michaelwucoc"},
    },
    "pusher": {"name": "Michaelwucoc"},
    "repository": {
        "full_name": "AWMC-TEAM/maimaiDX-QueryBot",
        "html_url": "https://github.com/AWMC-TEAM/maimaiDX-QueryBot",
    },
}
ENV = {
    "GITHUB_REPOSITORY": "AWMC-TEAM/maimaiDX-QueryBot",
    "GITHUB_RUN_ID": "12345",
    "GITHUB_SERVER_URL": "https://github.com",
}


running = MODULE.build_card(
    EVENT,
    {
        "state": "running",
        "deployed_commit": SHA,
        "bot_pid": 56303,
        "uptime_seconds": 3661,
    },
    ENV,
)
assert running["msg_type"] == "interactive"
assert running["card"]["header"]["template"] == "green"
assert running["card"]["header"]["title"]["content"] == "main 更新已部署"
content = running["card"]["elements"][0]["text"]["content"]
for expected in ("0123456", "fix: verify Feishu card", "56303", "1h 1m"):
    assert expected in content, expected

stopped = MODULE.build_card(
    EVENT,
    {"state": "stopped", "deployed_commit": "f" * 40},
    ENV,
)
assert stopped["card"]["header"]["template"] == "red"
assert "需要人工检查" in stopped["card"]["elements"][0]["text"]["content"]

assert MODULE._duration(59) == "59s"
assert MODULE._duration(90061) == "1d 1h 1m"

print("Feishu push notification checks: OK")
