#!/usr/bin/env python3
"""Send a Feishu card for a push to the main branch."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _first_line(value: str, limit: int = 180) -> str:
    line = value.splitlines()[0].strip() if value else "(no commit message)"
    return line if len(line) <= limit else f"{line[: limit - 3]}..."


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _duration(seconds: Any) -> str:
    try:
        remaining = max(0, int(seconds))
    except (TypeError, ValueError):
        return "unknown"

    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _commit_data(event: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    repository = event.get("repository") or {}
    head_commit = event.get("head_commit") or {}
    sha = str(event.get("after") or env.get("GITHUB_SHA") or _git("rev-parse", "HEAD"))
    repo_name = str(
        repository.get("full_name")
        or env.get("GITHUB_REPOSITORY")
        or _git("config", "--get", "remote.origin.url")
    )
    server_url = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo_url = str(repository.get("html_url") or f"{server_url}/{repo_name}")
    commit_url = str(head_commit.get("url") or f"{repo_url}/commit/{sha}")

    message = str(head_commit.get("message") or "")
    if not message:
        message = _git("log", "-1", "--pretty=%B")

    author = head_commit.get("author") or {}
    author_name = str(author.get("username") or author.get("name") or "")
    if not author_name:
        author_name = _git("log", "-1", "--pretty=%an")

    commits = event.get("commits") or []
    return {
        "sha": sha,
        "short_sha": sha[:7],
        "repo_name": repo_name,
        "repo_url": repo_url,
        "commit_url": commit_url,
        "message": _first_line(message),
        "author": author_name,
        "pusher": str((event.get("pusher") or {}).get("name") or author_name),
        "commit_count": len(commits) or 1,
        "compare_url": str(event.get("compare") or commit_url),
    }


def _status_view(status: dict[str, Any], pushed_sha: str) -> dict[str, str]:
    state = status.get("state", "unknown")
    deployed_sha = str(status.get("deployed_commit") or "")
    deployed_matches = bool(deployed_sha and deployed_sha == pushed_sha)
    qq_connected = status.get("qq_connected")

    if state == "running" and deployed_matches and qq_connected is True:
        return {
            "title": "main 更新已部署，QQ 已连接",
            "template": "green",
            "label": "🟢 运行中，已部署本次提交，QQ 已连接",
        }
    if state == "running" and deployed_matches:
        return {
            "title": "main 更新已部署，等待 QQ 连接",
            "template": "orange",
            "label": "🟡 运行中，已部署本次提交，等待 QQ 连接",
        }
    if state == "running":
        return {
            "title": "main 已更新，等待部署",
            "template": "orange",
            "label": "🟡 运行中，等待自动更新",
        }
    if state == "updating":
        return {
            "title": "main 已更新，Bot 更新中",
            "template": "orange",
            "label": "🟡 旧进程仍在服务，等待新进程启动",
        }
    if state == "restarting":
        return {
            "title": "main 已更新，Bot 重启中",
            "template": "orange",
            "label": "🟡 守护进程正常，Bot 正在重启",
        }
    if state == "stopped":
        return {
            "title": "main 已更新，Bot 已停止",
            "template": "red",
            "label": "🔴 已停止，需要人工检查",
        }
    return {
        "title": "main 已更新，Bot 状态未知",
        "template": "grey",
        "label": f"⚪ {status.get('detail') or '状态检查不可用'}",
    }


def build_card(
    event: dict[str, Any], status: dict[str, Any], env: dict[str, str]
) -> dict[str, Any]:
    commit = _commit_data(event, env)
    status_text = _status_view(status, commit["sha"])
    run_url = (
        f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{env.get('GITHUB_REPOSITORY', commit['repo_name'])}/actions/runs/"
        f"{env.get('GITHUB_RUN_ID', '')}"
    )
    deployed_sha = str(status.get("deployed_commit") or "unknown")
    deployed_display = deployed_sha[:7] if deployed_sha != "unknown" else deployed_sha
    details = (
        f"**仓库：** [{commit['repo_name']}]({commit['repo_url']})\n"
        f"**提交：** [{commit['short_sha']}]({commit['commit_url']}) "
        f"{commit['message']}\n"
        f"**作者 / 推送者：** {commit['author']} / {commit['pusher']}\n"
        f"**推送：** {commit['commit_count']} 个 commit → main\n"
        f"**机器人：** {status_text['label']}\n"
        f"**QQ 连接：** {'已连接' if status.get('qq_connected') is True else '等待连接' if status.get('qq_connected') is False else '未知'}\n"
        f"**生产版本：** {deployed_display} · PID "
        f"{status.get('bot_pid') or '-'} · 运行 "
        f"{_duration(status.get('uptime_seconds'))}"
    )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": status_text["template"],
                "title": {"tag": "plain_text", "content": status_text["title"]},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": details}},
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": "查看提交"},
                            "url": commit["commit_url"],
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "运行记录"},
                            "url": run_url,
                        },
                    ],
                },
            ],
        },
    }


def _add_signature(payload: dict[str, Any], secret: str) -> None:
    timestamp = str(int(time.time()))
    key = f"{timestamp}\n{secret}".encode()
    signature = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest())
    payload["timestamp"] = timestamp
    payload["sign"] = signature.decode()


def send(webhook_url: str, payload: dict[str, Any]) -> None:
    request = Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read().decode()
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"Feishu webhook returned HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Feishu webhook request failed: {exc.reason}") from exc

    result = json.loads(body)
    code = result.get("code", result.get("StatusCode", 0))
    if code != 0:
        raise RuntimeError(f"Feishu webhook rejected the message: {body}")



def build_pr_card(event: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Build a Feishu card for a pull_request event."""
    pr = event.get("pull_request") or {}
    repo = event.get("repository") or {}
    action = str(event.get("action") or "")

    repo_name = str(repo.get("full_name") or env.get("GITHUB_REPOSITORY") or "")
    repo_url = str(repo.get("html_url") or f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo_name}")
    pr_url = str(pr.get("html_url") or "")
    pr_number = pr.get("number") or env.get("GITHUB_REF_NAME", "").split("/")[0]
    pr_title = str(pr.get("title") or "(untitled)")
    pr_body = (str(pr.get("body") or "").strip())[:240]
    pr_state = str(pr.get("state") or "")
    merged = bool(pr.get("merged"))

    user = pr.get("user") or {}
    user_name = str(user.get("login") or "")

    base_ref = str((pr.get("base") or {}).get("ref") or "")
    head_ref = str((pr.get("head") or {}).get("ref") or "")

    action_labels = {
        "opened": ("PR 已创建", "blue"),
        "reopened": ("PR 已重新打开", "blue"),
        "closed": ("PR 已合并" if merged else "PR 已关闭", "green" if merged else "grey"),
        "synchronize": ("PR 有新提交", "orange"),
        "ready_for_review": ("PR 已准备好评审", "blue"),
        "review_requested": ("PR 请求评审", "blue"),
        "approved": ("PR 已通过评审", "green"),
        "merged": ("PR 已合并", "green"),
    }
    label, template = action_labels.get(action, (f"PR: {action}", "blue"))

    details_lines = [
        f"**仓库：** [{repo_name}]({repo_url})",
        f"**PR：** [#{pr_number} {pr_title}]({pr_url})",
        f"**分支：** `{head_ref}` → `{base_ref}`",
        f"**作者：** {user_name}",
        f"**状态：** {_pr_state_text(merged, pr_state, action)}",
        f"**操作：** {label}",
    ]
    if pr_body:
        details_lines.append("**描述：**")
        details_lines.append(pr_body)
    details = "\n".join(details_lines)

    run_url = (
        f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{env.get('GITHUB_REPOSITORY', repo_name)}/actions/runs/"
        f"{env.get('GITHUB_RUN_ID', '')}"
    )

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": details}},
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "查看 PR"},
                    "url": pr_url,
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "运行记录"},
                    "url": run_url,
                },
            ],
        },
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": f"[PR] {pr_title}"},
            },
            "elements": elements,
        },
    }


def _pr_state_text(merged: bool, pr_state: str, action: str) -> str:
    if merged:
        return "已合并"
    if pr_state == "closed":
        return "已关闭"
    if action == "ready_for_review":
        return "待评审"
    if action == "review_requested":
        return "请求评审"
    return "进行中"



def main() -> None:
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("FEISHU_WEBHOOK_URL is not configured; skipping Feishu notification")
        return

    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    event = json.loads(event_path.read_text(encoding="utf-8"))
    env_map = dict(os.environ)
    event_name = env_map.get("GITHUB_EVENT_NAME", "")
    if event_name in ("pull_request", "pull_request_target") or "pull_request" in event:
        payload = build_pr_card(event, env_map)
    else:
        status = json.loads(os.environ.get("BOT_STATUS_JSON") or '{"state":"unknown"}')
        payload = build_card(event, status, env_map)
    signing_secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    if signing_secret:
        _add_signature(payload, signing_secret)
    send(webhook_url, payload)
    print("Feishu notification sent")


if __name__ == "__main__":
    main()
