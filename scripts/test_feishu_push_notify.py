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
        "qq_connected": True,
        "bot_pid": 56303,
        "uptime_seconds": 3661,
    },
    ENV,
)
assert running["msg_type"] == "interactive"
assert running["card"]["header"]["template"] == "green"
assert running["card"]["header"]["title"]["content"] == "main 更新已部署，QQ 已连接"
content = running["card"]["elements"][0]["text"]["content"]
for expected in ("0123456", "fix: verify Feishu card", "56303", "1h 1m"):
    assert expected in content, expected
buttons = running["card"]["elements"][2]["actions"]
assert [button["text"]["content"] for button in buttons] == ["查看提交", "运行记录"]

waiting = MODULE.build_card(
    EVENT,
    {"state": "running", "deployed_commit": SHA, "qq_connected": False},
    ENV,
)
assert waiting["card"]["header"]["template"] == "orange"
assert "等待 QQ 连接" in waiting["card"]["header"]["title"]["content"]

stopped = MODULE.build_card(
    EVENT,
    {"state": "stopped", "deployed_commit": "f" * 40},
    ENV,
)
assert stopped["card"]["header"]["template"] == "red"
assert "需要人工检查" in stopped["card"]["elements"][0]["text"]["content"]

updating = MODULE.build_card(
    EVENT,
    {"state": "updating", "deployed_commit": SHA, "bot_pid": 56303},
    ENV,
)
assert updating["card"]["header"]["template"] == "orange"
assert "等待新进程启动" in updating["card"]["elements"][0]["text"]["content"]

assert MODULE._duration(59) == "59s"
assert MODULE._duration(90061) == "1d 1h 1m"


# PR card tests
PR_EVENT = {
    "action": "opened",
    "pull_request": {
        "number": 42,
        "title": "feat: add PR notification",
        "body": "This PR adds PR notifications to Feishu.",
        "html_url": "https://github.com/AWMC-TEAM/maimaiDX-QueryBot/pull/42",
        "state": "open",
        "merged": False,
        "user": {"login": "Michaelwucoc", "avatar_url": ""},
        "base": {"ref": "main"},
        "head": {"ref": "feat/pr-notify"},
    },
    "repository": {
        "full_name": "AWMC-TEAM/maimaiDX-QueryBot",
        "html_url": "https://github.com/AWMC-TEAM/maimaiDX-QueryBot",
    },
}

pr_opened = MODULE.build_pr_card(PR_EVENT, ENV)
assert pr_opened["msg_type"] == "interactive"
assert pr_opened["card"]["header"]["template"] == "blue"
assert "feat: add PR notification" in pr_opened["card"]["header"]["title"]["content"]
pr_content = pr_opened["card"]["elements"][0]["text"]["content"]
for expected in ("#42", "feat/pr-notify", "main", "Michaelwucoc", "PR 已创建"):
    assert expected in pr_content, expected
pr_buttons = pr_opened["card"]["elements"][2]["actions"]
assert [b["text"]["content"] for b in pr_buttons] == ["查看 PR", "运行记录"]

pr_merged = dict(PR_EVENT, action="closed")
pr_merged["pull_request"] = dict(PR_EVENT["pull_request"], state="closed", merged=True)
pr_merged_card = MODULE.build_pr_card(pr_merged, ENV)
assert pr_merged_card["card"]["header"]["template"] == "green"
assert "已合并" in pr_merged_card["card"]["elements"][0]["text"]["content"]

print("Feishu push notification checks: OK")
