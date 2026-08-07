#!/usr/bin/env python3
"""Feishu long-connection operations bot for maimaiDX QueryBot."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


LOG = logging.getLogger("maimaidx.feishu_ops")
SERVICE_NAME = "maimaidx-bot.service"
STATUS_SCRIPT = "/usr/local/bin/maimaidx-report-status"
MAX_LOG_LINES = 100
MAX_BREAK_GRANT = 100_000

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MENTION_RE = re.compile(r"@_user_\d+\s*")
MESSAGE_EVENT_RE = re.compile(
    r"(?=.*\[SUCCESS\])(?=.*\bEventType\.)(?=.*\bMessage\b)", re.I
)
SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|authorization|qrcode|app_secret)\b"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,}\]\"']+)"
)
QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:code|token|key|secret)=)[^&\s]+")
BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+")
ROBOT_TOKEN_RE = re.compile(r"ROBOT1\.0_[A-Za-z0-9._!~-]+")
FEISHU_ID_RE = re.compile(r"\b(?:ou|oc|on|cli)_[A-Za-z0-9]+\b")
QQ_ID_RE = re.compile(r"\bQQ\s+\d+\b")
GROUP_ID_RE = re.compile(r"\[Group:[^\]]+\]")


def parse_id_set(raw: str) -> frozenset[str]:
    return frozenset(item for item in re.split(r"[,\s;|]+", raw.strip()) if item)


@dataclass(frozen=True)
class OpsConfig:
    app_id: str
    app_secret: str = field(repr=False)
    allowed_chat_ids: frozenset[str]
    admin_open_ids: frozenset[str]
    admin_api_url: str
    admin_api_token: str = field(repr=False)
    state_db: Path

    @classmethod
    def from_env(cls) -> "OpsConfig":
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            allowed_chat_ids=parse_id_set(
                os.environ.get("FEISHU_OPS_ALLOWED_CHAT_IDS", "")
            ),
            admin_open_ids=parse_id_set(
                os.environ.get("FEISHU_OPS_ADMIN_OPEN_IDS", "")
            ),
            admin_api_url=os.environ.get(
                "MAIMAIDX_ADMIN_API_URL",
                "http://127.0.0.1:8099/maimaidx/admin/api",
            ).rstrip("/"),
            admin_api_token=os.environ.get("MAIMAIDX_ADMIN_API_TOKEN", "").strip(),
            state_db=Path(
                os.environ.get(
                    "FEISHU_OPS_STATE_DB",
                    "/var/lib/maimaidx-feishu-ops/actions.sqlite3",
                )
            ),
        )


def extract_text(content: str) -> str:
    try:
        text = str(json.loads(content).get("text") or "")
    except (json.JSONDecodeError, AttributeError):
        text = str(content or "")
    text = MENTION_RE.sub("", text)
    return " ".join(text.strip().split())


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "menu", []
    raw = parts[0].lower()
    aliases = {
        "帮助": "menu",
        "菜单": "menu",
        "help": "menu",
        "状态": "status",
        "status": "status",
        "日志": "logs",
        "logs": "logs",
        "错误": "errors",
        "errors": "errors",
        "管理": "admin",
        "重启bot": "restart",
        "重启": "restart",
        "启动bot": "start",
        "启动": "start",
        "停止bot": "stop",
        "停止": "stop",
        "查询break": "break_get",
        "发放break": "break_add",
        "设置break": "break_set",
        "封禁": "ban",
        "解封": "unban",
        "身份": "identity",
        "whoami": "identity",
    }
    return aliases.get(raw, raw), parts[1:]


def redact_log_line(line: str) -> str:
    value = ANSI_RE.sub("", line).replace("```", "''' ")
    value = BEARER_RE.sub("Bearer [REDACTED]", value)
    value = SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
    )
    value = QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = ROBOT_TOKEN_RE.sub("ROBOT1.0_[REDACTED]", value)
    value = FEISHU_ID_RE.sub(lambda match: f"{match.group(0)[:3]}[REDACTED]", value)
    value = QQ_ID_RE.sub("QQ [REDACTED]", value)
    value = GROUP_ID_RE.sub("[Group:REDACTED]", value)
    return value[:700]


def sanitize_logs(lines: list[str], *, errors_only: bool = False) -> list[str]:
    result: list[str] = []
    for line in lines:
        if MESSAGE_EVENT_RE.search(line):
            continue
        if errors_only and not re.search(
            r"\b(WARNING|WARN|ERROR|CRITICAL)\b|Traceback|Exception", line, re.I
        ):
            continue
        cleaned = redact_log_line(line).strip()
        if cleaned:
            result.append(cleaned)
    return result


def format_duration(seconds: Any) -> str:
    try:
        remaining = max(0, int(seconds))
    except (TypeError, ValueError):
        return "未知"
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    if not parts:
        parts.append(f"{seconds}秒")
    return " ".join(parts)


def _button(text: str, action: str, *, primary: bool = False, **value: Any) -> dict:
    payload = {"action": action, **value}
    button = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "value": payload,
    }
    if primary:
        button["type"] = "primary"
    return button


def _card(title: str, content: str, *, template: str = "blue", actions=None) -> dict:
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": content}}
    ]
    if actions:
        elements.extend([{"tag": "hr"}, {"tag": "action", "actions": actions}])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def menu_card(*, is_admin: bool) -> dict:
    content = (
        "**群成员命令**\n"
        "`状态` · `日志 30` · `错误 30` · `菜单`\n\n"
        "日志会过滤聊天正文并脱敏用户标识和凭据。"
    )
    actions = [
        _button("运行状态", "status", primary=True),
        _button("最近日志", "logs", limit=30),
        _button("最近错误", "errors", limit=30),
    ]
    if is_admin:
        actions.append(_button("管理操作", "admin_menu"))
        content += (
            "\n\n**管理员命令**\n"
            "`重启Bot` · `启动Bot` · `停止Bot`\n"
            "`查询BREAK <QQ>` · `发放BREAK <QQ> <数量>` · "
            "`设置BREAK <QQ> <余额>`\n"
            "`封禁 <用户ID> <小时> <原因>` · `解封 <用户ID>`"
        )
    return _card("AWMC Bot 运维菜单", content, actions=actions)


def bootstrap_card(chat_id: str, open_id: str) -> dict:
    return _card(
        "运维机器人等待授权",
        "管理员尚未配置允许的群和用户。请将下面两项交给部署管理员：\n"
        f"**chat_id:** `{chat_id}`\n"
        f"**open_id:** `{open_id}`",
        template="orange",
    )


def status_card(status: dict[str, Any], *, is_admin: bool) -> dict:
    state = str(status.get("state") or "unknown")
    state_map = {
        "running": ("🟢 运行中", "green"),
        "updating": ("🟡 更新中", "orange"),
        "restarting": ("🟡 重启中", "orange"),
        "stopped": ("🔴 已停止", "red"),
    }
    state_text, template = state_map.get(state, ("⚪ 状态未知", "grey"))
    sha = str(status.get("deployed_commit") or "-")[:7]
    rss_mib = float(status.get("rss_kib") or 0) / 1024
    disk_percent = float(status.get("disk_percent") or 0)
    content = (
        f"**Bot：** {state_text}\n"
        f"**systemd：** `{status.get('active_state', '-')}` / "
        f"`{status.get('sub_state', '-')}` · Result `{status.get('result', '-')}`\n"
        f"**进程：** Supervisor `{status.get('supervisor_pid') or '-'}` · "
        f"NoneBot `{status.get('bot_pid') or '-'}`\n"
        f"**运行时长：** {format_duration(status.get('uptime_seconds'))}\n"
        f"**资源：** CPU `{status.get('cpu_percent', 0)}%` · "
        f"内存 `{rss_mib:.1f} MiB` · 磁盘 `{disk_percent:.1f}%`\n"
        f"**系统负载：** `{status.get('load_1', 0):.2f}` / "
        f"`{status.get('load_5', 0):.2f}` / `{status.get('load_15', 0):.2f}`\n"
        f"**生产版本：** `{sha}` · systemd 重启 `{status.get('n_restarts', 0)}` 次"
    )
    actions = [
        _button("刷新", "status", primary=True),
        _button("日志", "logs", limit=30),
        _button("错误", "errors", limit=30),
    ]
    if is_admin:
        actions.append(_button("管理", "admin_menu"))
    return _card("AWMC Bot 运行状态", content, template=template, actions=actions)


def logs_card(lines: list[str], *, errors_only: bool, is_admin: bool) -> dict:
    label = "错误日志" if errors_only else "运行日志"
    body = "\n".join(lines[-MAX_LOG_LINES:]) or "没有匹配的日志。"
    body = body[-7000:]
    actions = [
        _button("刷新", "errors" if errors_only else "logs", primary=True, limit=30),
        _button("运行状态", "status"),
    ]
    if is_admin:
        actions.append(_button("管理", "admin_menu"))
    return _card(
        f"AWMC Bot {label}",
        f"已脱敏，最多显示 {MAX_LOG_LINES} 行。\n```text\n{body}\n```",
        template="red" if errors_only and lines else "blue",
        actions=actions,
    )


def admin_menu_card() -> dict:
    return _card(
        "AWMC Bot 管理操作",
        "Bot 启停操作需要二次确认。BREAK 与封禁操作请使用管理员命令，"
        "机器人会生成带一次性请求 ID 的确认卡片。",
        template="orange",
        actions=[
            _button("重启 Bot", "control_prepare", operation="restart"),
            _button("启动 Bot", "control_prepare", operation="start"),
            _button("停止 Bot", "control_prepare", operation="stop"),
            _button("运行状态", "status"),
        ],
    )


def confirmation_card(title: str, summary: str, action: str, **value: Any) -> dict:
    request_id = value.pop("request_id", uuid4().hex)
    return _card(
        title,
        f"{summary}\n\n请求 ID：`{request_id[:12]}`",
        template="red",
        actions=[
            _button(
                "确认执行",
                action,
                primary=True,
                request_id=request_id,
                **value,
            ),
            _button("取消", "admin_menu"),
        ],
    )


class ActionStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS actions ("
                "request_id TEXT PRIMARY KEY, action TEXT NOT NULL, "
                "actor TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    def claim(self, request_id: str, action: str, actor: str) -> bool:
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "INSERT INTO actions(request_id, action, actor, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (request_id, action, actor, time.time()),
                )
            return True
        except sqlite3.IntegrityError:
            return False


class SystemController:
    @staticmethod
    def _run(args: list[str], *, timeout: int = 15) -> str:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()

    def status(self) -> dict[str, Any]:
        report = json.loads(self._run([STATUS_SCRIPT]))
        properties = self._run(
            [
                "systemctl",
                "show",
                SERVICE_NAME,
                "--property=ActiveState,SubState,Result,NRestarts,MainPID",
            ]
        )
        for line in properties.splitlines():
            key, _, value = line.partition("=")
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()
            report[snake] = int(value) if key in {"NRestarts", "MainPID"} else value

        bot_pid = report.get("bot_pid")
        report.update({"cpu_percent": 0.0, "rss_kib": 0})
        if bot_pid:
            try:
                process = self._run(
                    ["ps", "-p", str(bot_pid), "-o", "%cpu=,rss="], timeout=5
                ).split()
                report["cpu_percent"] = float(process[0])
                report["rss_kib"] = int(process[1])
            except (IndexError, ValueError, subprocess.SubprocessError):
                pass
        load = os.getloadavg()
        report.update({"load_1": load[0], "load_5": load[1], "load_15": load[2]})
        usage = shutil.disk_usage("/www/bot")
        report["disk_percent"] = usage.used / usage.total * 100
        return report

    def logs(self, limit: int, *, errors_only: bool) -> list[str]:
        limit = min(MAX_LOG_LINES, max(1, int(limit)))
        raw = self._run(
            [
                "journalctl",
                "-u",
                SERVICE_NAME,
                "-n",
                str(max(limit * 5, 100)),
                "--no-pager",
                "-o",
                "short-iso",
            ],
            timeout=10,
        )
        return sanitize_logs(raw.splitlines(), errors_only=errors_only)[-limit:]

    def control(self, operation: str) -> None:
        if operation not in {"start", "stop", "restart"}:
            raise ValueError("unsupported service operation")
        self._run(["sudo", "systemctl", operation, SERVICE_NAME], timeout=40)


class AdminClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def _request(
        self, path: str, *, method: str = "GET", payload: dict | None = None
    ) -> Any:
        if not self.token:
            raise RuntimeError("MAIMAIDX_ADMIN_API_TOKEN is not configured")
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Admin API HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Admin API unavailable: {exc.reason}") from exc

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        query = urlencode({"search": user_id, "limit": 20})
        rows = self._request(f"/users?{query}")
        return next((row for row in rows if str(row.get("user_id")) == user_id), None)

    def update_break(self, user_id: str, amount: int, mode: str, actor: str) -> dict:
        return self._request(
            f"/users/{quote(user_id, safe='')}/break",
            method="POST",
            payload={
                "mode": mode,
                "amount": amount,
                "source": "feishu_ops",
                "actor": actor,
            },
        )

    def ban(self, user_id: str, hours: float, reason: str, actor: str) -> dict:
        return self._request(
            f"/users/{quote(user_id, safe='')}/ban",
            method="POST",
            payload={
                "hours": hours,
                "reason": reason,
                "source": "feishu_ops",
                "actor": actor,
            },
        )

    def unban(self, user_id: str, actor: str) -> dict:
        query = urlencode({"source": "feishu_ops", "actor": actor})
        return self._request(
            f"/users/{quote(user_id, safe='')}/ban?{query}", method="DELETE"
        )


class FeishuOpsBot:
    def __init__(self, config: OpsConfig, lark_module: Any):
        self.config = config
        self.lark = lark_module
        self.system = SystemController()
        self.admin = AdminClient(config.admin_api_url, config.admin_api_token)
        self.actions = ActionStore(config.state_db)
        self.client = (
            lark_module.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .log_level(lark_module.LogLevel.WARNING)
            .build()
        )

    def is_admin(self, open_id: str) -> bool:
        return open_id in self.config.admin_open_ids

    def is_allowed_chat(self, chat_id: str) -> bool:
        return chat_id in self.config.allowed_chat_ids

    def _reply_card(self, message_id: str, card: dict) -> None:
        im = self.lark.im.v1
        request = (
            im.ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                im.ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(f"Feishu reply failed: {response.code} {response.msg}")

    def _send_card(self, receive_id_type: str, receive_id: str, card: dict) -> None:
        im = self.lark.im.v1
        request = (
            im.CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                im.CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .uuid(uuid4().hex)
                .build()
            )
            .build()
        )
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"Feishu send failed: {response.code} {response.msg}")

    @staticmethod
    def _response(card: dict | None = None, toast: str = "") -> dict:
        payload: dict[str, Any] = {}
        if toast:
            payload["toast"] = {"type": "info", "content": toast}
        if card is not None:
            payload["card"] = {"type": "raw", "data": card}
        return payload

    def _safe_status_card(self, is_admin: bool) -> dict:
        try:
            return status_card(self.system.status(), is_admin=is_admin)
        except Exception as exc:
            LOG.exception("status query failed")
            return _card(
                "状态查询失败",
                f"`{type(exc).__name__}`：{str(exc)[:300]}",
                template="red",
            )

    def _safe_logs_card(self, limit: int, errors_only: bool, is_admin: bool) -> dict:
        try:
            lines = self.system.logs(limit, errors_only=errors_only)
            return logs_card(lines, errors_only=errors_only, is_admin=is_admin)
        except Exception as exc:
            LOG.exception("log query failed")
            return _card(
                "日志查询失败",
                f"`{type(exc).__name__}`：{str(exc)[:300]}",
                template="red",
            )

    def handle_message(self, data: Any) -> None:
        event = data.event
        message = event.message
        sender = event.sender.sender_id
        chat_id = str(message.chat_id or "")
        open_id = str(sender.open_id or "")
        if message.message_type != "text":
            return
        if not self.config.allowed_chat_ids:
            self._reply_card(message.message_id, bootstrap_card(chat_id, open_id))
            return
        if not self.is_allowed_chat(chat_id):
            return
        command, args = parse_command(extract_text(message.content))
        is_admin = self.is_admin(open_id)
        try:
            card = self._dispatch_command(command, args, open_id, is_admin)
        except Exception as exc:
            LOG.exception("command failed: %s", command)
            card = _card(
                "操作失败", f"`{type(exc).__name__}`：{str(exc)[:300]}", template="red"
            )
        self._reply_card(message.message_id, card)

    def _dispatch_command(
        self, command: str, args: list[str], open_id: str, is_admin: bool
    ) -> dict:
        if command == "menu":
            return menu_card(is_admin=is_admin)
        if command == "identity":
            return _card("飞书身份", f"open_id：`{open_id}`")
        if command == "status":
            return self._safe_status_card(is_admin)
        if command in {"logs", "errors"}:
            limit = min(MAX_LOG_LINES, max(1, int(args[0]) if args else 30))
            return self._safe_logs_card(limit, command == "errors", is_admin)
        if not is_admin:
            return _card("权限不足", "该操作仅限飞书运维管理员。", template="red")
        if command == "admin":
            return admin_menu_card()
        if command in {"start", "stop", "restart"}:
            labels = {"start": "启动", "stop": "停止", "restart": "重启"}
            return confirmation_card(
                f"确认{labels[command]} Bot",
                f"即将对 `{SERVICE_NAME}` 执行 `{command}`。",
                "control_confirm",
                operation=command,
            )
        if command == "break_get":
            if len(args) != 1 or not args[0].isdigit():
                raise ValueError("用法：查询BREAK <QQ号>")
            row = self.admin.get_user(args[0])
            if row is None:
                return _card(
                    "BREAK 查询", f"未找到用户 `{args[0]}`。", template="orange"
                )
            return _card(
                "BREAK 查询",
                f"用户：`{args[0]}`\n余额：**{row.get('break', 0)} BREAK**\n"
                f"封禁：`{bool(row.get('banned'))}`",
            )
        if command in {"break_add", "break_set"}:
            if len(args) != 2 or not args[0].isdigit():
                raise ValueError(
                    "用法：发放BREAK <QQ号> <正整数> 或 设置BREAK <QQ号> <余额>"
                )
            amount = int(args[1])
            if command == "break_add" and not 1 <= amount <= MAX_BREAK_GRANT:
                raise ValueError(f"单次发放范围为 1～{MAX_BREAK_GRANT}")
            if command == "break_set" and not -1_000_000 <= amount <= 1_000_000:
                raise ValueError("余额设置范围为 -1000000～1000000")
            mode = "add" if command == "break_add" else "set"
            verb = "发放" if mode == "add" else "设置余额为"
            return confirmation_card(
                "确认 BREAK 操作",
                f"目标用户：`{args[0]}`\n{verb}：**{amount} BREAK**",
                "break_confirm",
                user_id=args[0],
                amount=amount,
                mode=mode,
            )
        if command == "ban":
            if len(args) < 2:
                raise ValueError("用法：封禁 <用户ID> <小时，0=永久> [原因]")
            hours = float(args[1])
            if not 0 <= hours <= 24 * 365:
                raise ValueError("封禁时长范围为 0～8760 小时")
            reason = " ".join(args[2:]).strip() or "飞书管理员封禁"
            return confirmation_card(
                "确认封禁用户",
                f"用户：`{args[0]}`\n时长：`{'永久' if hours == 0 else f'{hours:g} 小时'}`\n"
                f"原因：{reason[:200]}",
                "ban_confirm",
                user_id=args[0],
                hours=hours,
                reason=reason[:200],
            )
        if command == "unban":
            if len(args) != 1:
                raise ValueError("用法：解封 <用户ID>")
            return confirmation_card(
                "确认解封用户",
                f"用户：`{args[0]}`",
                "unban_confirm",
                user_id=args[0],
            )
        return menu_card(is_admin=is_admin)

    def handle_card_action(self, data: Any) -> dict:
        event = data.event
        open_id = str(event.operator.open_id or "")
        chat_id = str(event.context.open_chat_id or "")
        value = dict(event.action.value or {})
        action = str(value.get("action") or "")
        is_admin = self.is_admin(open_id)
        if not self.is_allowed_chat(chat_id) and not is_admin:
            return self._response(toast="该群未获授权")
        if action == "status":
            return self._response(self._safe_status_card(is_admin), "状态已刷新")
        if action in {"logs", "errors"}:
            limit = min(MAX_LOG_LINES, max(1, int(value.get("limit") or 30)))
            return self._response(
                self._safe_logs_card(limit, action == "errors", is_admin), "日志已刷新"
            )
        if not is_admin:
            return self._response(toast="该操作仅限管理员")
        if action == "admin_menu":
            return self._response(admin_menu_card())
        if action == "control_prepare":
            operation = str(value.get("operation") or "")
            labels = {"start": "启动", "stop": "停止", "restart": "重启"}
            if operation not in labels:
                return self._response(toast="未知操作")
            return self._response(
                confirmation_card(
                    f"确认{labels[operation]} Bot",
                    f"即将对 `{SERVICE_NAME}` 执行 `{operation}`。",
                    "control_confirm",
                    operation=operation,
                )
            )
        if action == "control_confirm":
            return self._handle_control(value, open_id, chat_id)
        if action == "break_confirm":
            return self._handle_break(value, open_id)
        if action == "ban_confirm":
            return self._handle_ban(value, open_id, unban=False)
        if action == "unban_confirm":
            return self._handle_ban(value, open_id, unban=True)
        return self._response(toast="未知操作")

    def _claim(self, value: dict, action: str, actor: str) -> bool:
        request_id = str(value.get("request_id") or "")
        return bool(request_id) and self.actions.claim(request_id, action, actor)

    def _handle_control(self, value: dict, actor: str, chat_id: str) -> dict:
        operation = str(value.get("operation") or "")
        if operation not in {"start", "stop", "restart"}:
            return self._response(toast="未知服务操作")
        if not self._claim(value, f"control.{operation}", actor):
            return self._response(toast="该请求已经执行或已失效")

        def worker() -> None:
            try:
                self.system.control(operation)
                time.sleep(4 if operation != "stop" else 1)
                card = self._safe_status_card(True)
            except Exception as exc:
                LOG.exception("service control failed")
                card = _card(
                    "Bot 管理失败",
                    f"`{type(exc).__name__}`：{str(exc)[:300]}",
                    template="red",
                )
            if chat_id:
                try:
                    self._send_card("chat_id", chat_id, card)
                except Exception:
                    LOG.exception("failed to send service control result")

        threading.Thread(
            target=worker, name=f"control-{operation}", daemon=True
        ).start()
        return self._response(
            _card("操作已提交", f"正在执行 `{operation}`，结果会发送到当前群。"),
            "操作已提交",
        )

    def _handle_break(self, value: dict, actor: str) -> dict:
        if not self._claim(value, "break.update", actor):
            return self._response(toast="该请求已经执行或已失效")
        user_id = str(value.get("user_id") or "")
        amount = int(value.get("amount"))
        mode = str(value.get("mode") or "")
        if not user_id.isdigit() or mode not in {"add", "set"}:
            return self._response(toast="BREAK 参数无效")
        if mode == "add" and not 1 <= amount <= MAX_BREAK_GRANT:
            return self._response(toast="BREAK 数量超出范围")
        result = self.admin.update_break(user_id, amount, mode, actor)
        return self._response(
            _card(
                "BREAK 操作完成",
                f"用户：`{user_id}`\n当前余额：**{result['balance']} BREAK**\n"
                f"REF_ID：`{result.get('ref_id', '-')}`",
                template="green",
            ),
            "BREAK 已更新",
        )

    def _handle_ban(self, value: dict, actor: str, *, unban: bool) -> dict:
        action = "user.unban" if unban else "user.ban"
        if not self._claim(value, action, actor):
            return self._response(toast="该请求已经执行或已失效")
        user_id = str(value.get("user_id") or "").strip()
        if not user_id or len(user_id) > 128:
            return self._response(toast="用户 ID 无效")
        if unban:
            result = self.admin.unban(user_id, actor)
            label = "解封"
        else:
            result = self.admin.ban(
                user_id,
                float(value.get("hours") or 0),
                str(value.get("reason") or "飞书管理员封禁")[:200],
                actor,
            )
            label = "封禁"
        return self._response(
            _card(
                f"用户{label}完成",
                f"用户：`{user_id}`\nREF_ID：`{result.get('ref_id', '-')}`",
                template="green",
            ),
            f"已{label}",
        )

    def handle_menu_event(self, data: Any) -> None:
        event = data.event
        operator_id = event.operator.operator_id
        open_id = str(operator_id.open_id or "")
        if not self.is_admin(open_id):
            return
        key = str(event.event_key or "")
        cards = {
            "ops_status": lambda: self._safe_status_card(True),
            "ops_logs": lambda: self._safe_logs_card(30, False, True),
            "ops_errors": lambda: self._safe_logs_card(30, True, True),
            "ops_admin": admin_menu_card,
        }
        factory = cards.get(key)
        if factory:
            self._send_card("open_id", open_id, factory())


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("FEISHU_OPS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
    except ImportError as exc:
        raise SystemExit("lark-oapi is required: pip install lark-oapi") from exc

    config = OpsConfig.from_env()
    bot = FeishuOpsBot(config, lark)

    def on_message(data: Any) -> None:
        try:
            bot.handle_message(data)
        except Exception:
            LOG.exception("message handler failed")

    def on_card(data: Any) -> Any:
        try:
            payload = bot.handle_card_action(data)
        except Exception as exc:
            LOG.exception("card handler failed")
            payload = {"toast": {"type": "error", "content": str(exc)[:100]}}
        return P2CardActionTriggerResponse(payload)

    def on_menu(data: Any) -> None:
        try:
            bot.handle_menu_event(data)
        except Exception:
            LOG.exception("menu handler failed")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card)
        .register_p2_application_bot_menu_v6(on_menu)
        .build()
    )
    LOG.info(
        "starting Feishu ops bot; allowed_chats=%d admins=%d",
        len(config.allowed_chat_ids),
        len(config.admin_open_ids),
    )
    client = lark.ws.Client(
        config.app_id,
        config.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.WARNING,
    )
    try:
        client.start()
    except KeyboardInterrupt:
        LOG.info("Feishu ops bot stopped")


if __name__ == "__main__":
    main()
