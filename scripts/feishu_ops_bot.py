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
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


LOG = logging.getLogger("maimaidx.feishu_ops")
SERVICE_NAME = "maimaidx-bot.service"
STATUS_SCRIPT = "/usr/local/bin/maimaidx-report-status"
MAX_LOG_LINES = 50
# 刷新时一次性拉取的原始行数上限（防 journalctl 超时）。
# 5000 行 / 50 每页 = 100 页，翻页足够。
LOG_FETCH_LINES_ALL = 5000
LOG_FETCH_TIMEOUT_SECONDS = 30
# 默认日志窗口：当前往前 10 分钟。窗口小 + -n 上限，journalctl 同步秒级返回。
LOG_WINDOW_DEFAULT_SECS = 600
# 快照过期阈值：超过此时间未刷新则下次走全量，避免漏数据。
LOG_SNAPSHOT_STALE_SECS = 600
MAX_BREAK_GRANT = 100_000


_WINDOW_RE = re.compile(r"^\s*(\d+)\s*(m|h)\s*$", re.IGNORECASE)


def parse_window_secs(text: str) -> Optional[int]:
    """解析日志窗口参数：支持「2h」「1h」「10m」「30m」，返回秒数。

    仅允许 m（分钟）和 h（小时）。非法返回 None。
    """
    if not text:
        return None
    m = _WINDOW_RE.match(text)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    return n * 60 if m.group(2).lower() == "m" else n * 3600


@dataclass
class LogSnapshot:
    """日志翻页快照。翻页只移动 cursor，不重新调 journalctl；刷新走「去头填尾」增量。

    lines 按时间正序（旧→新）存储，lines[-page_size:] 即最新一页。
    current_page=0 表示最新一页，正向递增表示更早的页。
    last_ts 为 lines 最后一行的 short-iso 时间戳，用于增量去重；
    无行时为空串，下次刷新走全量。
    window_secs 为本快照对应的日志窗口秒数（用于刷新时复用窗口）。
    """

    lines: list[str]
    errors_only: bool
    fetched_at: float
    window_secs: int = LOG_WINDOW_DEFAULT_SECS
    page_size: int = MAX_LOG_LINES
    current_page: int = 0
    last_ts: str = ""

    def total_pages(self) -> int:
        if not self.lines:
            return 1
        return max(1, (len(self.lines) + self.page_size - 1) // self.page_size)

    def current_lines(self) -> list[str]:
        if not self.lines:
            return []
        total = len(self.lines)
        end = total - self.current_page * self.page_size
        start = max(0, end - self.page_size)
        return self.lines[start:end]

    def go_prev(self) -> bool:
        """向更早翻一页。已到最早一页返回 False。"""
        if self.current_page + 1 >= self.total_pages():
            return False
        self.current_page += 1
        return True

    def go_next(self) -> bool:
        """向更新翻一页。已在最新一页返回 False。"""
        if self.current_page <= 0:
            return False
        self.current_page -= 1
        return True


# short-iso 每行以「YYYY-MM-DD HH:MM:SS」开头
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _line_ts(line: str) -> str:
    """提取日志行的 short-iso 时间戳，无匹配返回空串。"""
    m = _LOG_TS_RE.match(line)
    return m.group(1) if m else ""

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MENTION_RE = re.compile(r"@_user_\d+\s*")
MESSAGE_EVENT_RE = re.compile(
    r"(?:\[EventType\.[^\]]*(?:MESSAGE|MESSAGE_CREATE)|"
    r"Message\s+[^\n]{0,180}\s+from\s+[0-9A-F]{8,}|"
    r"\[Text\(type='text'|\[Attachment\(type=)",
    re.I,
)
PROCESSING_NOISE_RE = re.compile(
    r"(?:Event will be handled by Matcher|Matcher\(type=.*\) running complete)",
    re.I,
)
EXPECTED_404_RE = re.compile(
    r"nonebot_plugin_maimaidx.*\[wmc\].*status[=:] ?404\b", re.I
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
    super_admin_open_ids: frozenset[str]
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
            super_admin_open_ids=parse_id_set(
                os.environ.get("FEISHU_OPS_SUPER_ADMIN_OPEN_IDS", "")
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
        "概览": "overview",
        "业务概览": "overview",
        "指令调用": "commands",
        "指令统计": "commands",
        "命令统计": "commands",
        "查询ref": "ref_query",
        "查询refid": "ref_query",
        "ref": "ref_query",
        "refid": "ref_query",
        "今日token": "analysis_tokens",
        "今日锐评token": "analysis_tokens",
        "锐评token": "analysis_tokens",
        "token消耗": "analysis_tokens",
        "消息量": "messages",
        "消息统计": "messages",
        "awmcapi": "api_report",
        "api统计": "api_report",
        "api调用": "api_report",
        "管理": "admin",
        "权限": "permissions",
        "权限管理": "permissions",
        "管理员列表": "permissions",
        "授权管理员": "grant_admin",
        "撤销管理员": "revoke_admin",
        "重启bot": "restart",
        "重启": "restart",
        "启动bot": "start",
        "启动": "start",
        "停止bot": "stop",
        "停止": "stop",
        "查询break": "break_get",
        "发放break": "break_add",
        "设置break": "break_set",
        "卡密统计": "card_stats",
        "创建卡密": "card_create",
        "查询卡密": "card_get",
        "卡密查询": "card_get",
        "作废卡密": "card_disable",
        "封禁": "ban",
        "解封": "unban",
        "封禁列表": "ban_list",
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


CARD_TYPE_ALIASES = {
    "1": "break", "b": "break", "break": "break", "break卡": "break",
    "2": "double_break", "double": "double_break", "双倍": "double_break",
    "双倍break": "double_break", "双倍break卡": "double_break",
    "3": "freedom", "f": "freedom", "freedom": "freedom", "freedom卡": "freedom",
}
CARD_TYPE_LABELS = {
    "break": "BREAK 卡",
    "double_break": "双倍 BREAK 卡",
    "freedom": "FREEDOM 卡",
}


def resolve_card_type(text: str) -> str | None:
    return CARD_TYPE_ALIASES.get(str(text or "").strip().lower().replace(" ", ""))


def parse_card_duration(text: str) -> int:
    raw = str(text or "").strip().lower()
    if raw.isdigit():
        seconds = int(raw)
        if seconds <= 0:
            raise ValueError("时长必须大于 0")
        return seconds
    unit_map = {"": 1, "s": 1, "秒": 1, "m": 60, "分": 60,
                "h": 3600, "时": 3600, "小时": 3600, "d": 86400, "天": 86400}
    total = 0
    matched = False
    for number, unit in re.findall(r"(\d+)\s*(天|小时|时|分|秒|[dhms])?", raw):
        matched = True
        factor = unit_map.get(unit.lower() if unit else "", 0)
        if not factor:
            raise ValueError(f"无法识别的时间单位：{unit}")
        total += int(number) * factor
    if not matched or total <= 0:
        raise ValueError("时长格式不正确，例如 7d、24h、30m")
    return total




def sanitize_logs(lines: list[str], *, errors_only: bool = False) -> list[str]:
    result: list[str] = []
    for line in lines:
        if MESSAGE_EVENT_RE.search(line) or PROCESSING_NOISE_RE.search(line):
            continue
        if EXPECTED_404_RE.search(line):
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


def _card(
    title: str,
    content: str,
    *,
    template: str = "blue",
    actions=None,
    include_menu: bool = True,
    plain_text: bool = False,
) -> dict:
    card_actions = list(actions or [])
    if include_menu and not any(
        str(item.get("value", {}).get("action") or "") == "menu"
        for item in card_actions
        if isinstance(item, dict)
    ):
        card_actions.append(_button("主菜单", "menu"))
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text" if plain_text else "lark_md",
                "content": content,
            },
        }
    ]
    if card_actions:
        elements.append({"tag": "hr"})
        for offset in range(0, len(card_actions), 5):
            elements.append(
                {"tag": "action", "actions": card_actions[offset : offset + 5]}
            )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def menu_card(*, is_admin: bool, is_super_admin: bool = False) -> dict:
    content = (
        "**群成员命令**\n"
        "状态 · 日志 30 · 错误 30 · 菜单\n\n"
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
            "概览 · 指令调用 · 消息量 [1|3|7|30] · 今日锐评Token · AWMC API\n"
            "查询REF <REF_ID>\n"
            "重启Bot · 启动Bot · 停止Bot\n"
            "查询BREAK <QQ> · 发放BREAK <QQ> <数量> · "
            "设置BREAK <QQ> <余额>\n"
            "卡密统计 · 查询卡密 <卡密/批次> · "
            "创建卡密 <1/2/3> <面值/时长> [数量] [备注] · 作废卡密 <卡密>\n"
            "封禁 <用户ID> <小时> <原因> · 解封 <用户ID>\n"
            "封禁列表 [全部]"
        )
        if is_super_admin:
            content += "\n权限管理 · 授权管理员 <open_id> · 撤销管理员 <open_id>"
        actions.extend(
            [
                _button("业务概览", "overview"),
                _button("指令调用", "commands"),
                _button("消息量", "messages", days=1),
                _button("锐评 Token", "analysis_tokens"),
                _button("AWMC API", "api_report"),
            ]
        )
    return _card(
        "AWMC Bot 运维菜单", content, template="turquoise", actions=actions,
        include_menu=False
    )


def bootstrap_card(chat_id: str, open_id: str) -> dict:
    return _card(
        "运维机器人等待授权",
        "管理员尚未配置允许的群和用户。请将下面两项交给部署管理员：\n"
        f"**chat_id:** {chat_id}\n"
        f"**open_id:** {open_id}",
        template="orange",
    )


def status_card(status: dict[str, Any], *, is_admin: bool) -> dict:
    state = str(status.get("state") or "unknown")
    qq_connected = status.get("qq_connected")
    process_text = {
        "running": "🟢 运行中",
        "updating": "🟡 更新中",
        "restarting": "🟡 重启中",
        "stopped": "🔴 已停止",
    }.get(state, "⚪ 状态未知")
    if state == "running" and qq_connected is True:
        template = "green"
        qq_text = "🟢 QQ 已连接"
    elif state == "running":
        template = "orange"
        qq_text = "🟡 等待 QQ 连接"
    elif state in {"updating", "restarting"}:
        template = "orange"
        qq_text = "🟡 等待 QQ 连接"
    else:
        template = "red"
        qq_text = "🔴 QQ 未连接"
    sha = str(status.get("deployed_commit") or "-")[:7]
    rss_mib = float(status.get("rss_kib") or 0) / 1024
    disk_percent = float(status.get("disk_percent") or 0)
    content = (
        f"**运行状态：** {process_text}\n"
        f"**部署状态：** 已部署本次提交 {sha}\n"
        f"**QQ 连接：** {qq_text}\n"
        f"**systemd：** {status.get('active_state', '-')} / "
        f"{status.get('sub_state', '-')} · Result {status.get('result', '-')}\n"
        f"**进程：** Supervisor {status.get('supervisor_pid') or '-'} · "
        f"NoneBot {status.get('bot_pid') or '-'}\n"
        f"**运行时长：** {format_duration(status.get('uptime_seconds'))}\n"
        f"**资源：** CPU {status.get('cpu_percent', 0)}% · "
        f"内存 {rss_mib:.1f} MiB · 磁盘 {disk_percent:.1f}%\n"
        f"**系统负载：** {status.get('load_1', 0):.2f} / "
        f"{status.get('load_5', 0):.2f} / {status.get('load_15', 0):.2f}\n"
        f"**生产版本：** {sha} · systemd 重启 {status.get('n_restarts', 0)} 次"
    )
    actions = [
        _button("刷新", "status", primary=True),
        _button("日志", "logs", limit=30),
        _button("错误", "errors", limit=30),
    ]
    if is_admin:
        actions.extend(
            [
                _button("管理", "admin_menu"),
                _button("业务概览", "overview"),
            ]
        )
    return _card("AWMC Bot 运行状态", content, template=template, actions=actions)


def _fmt_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def command_stats_card(rows: list[dict[str, Any]]) -> dict:
    lines = ["**近 7 日指令调用**"]
    for row in rows[:20]:
        lines.append(
            f"{str(row.get('command') or '-')[:40]}：{_fmt_count(row.get('calls'))} 次，"
            f"成功 {_fmt_count(row.get('success'))}，错误 {_fmt_count(row.get('errors'))}，"
            f"平均 {row.get('avg_ms', 0)} ms"
        )
    return _card(
        "指令调用统计",
        "\n".join(lines) if len(lines) > 1 else "暂无指令审计数据。",
        template="indigo",
    )


def ref_card(trace: dict[str, Any] | None, ref_id: str) -> dict:
    if not trace:
        return _card("REF_ID 查询", f"未找到 {ref_id}。", template="orange")

    def detail_text(value: Any, limit: int = 500) -> str:
        if value in (None, ""):
            return "-"
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                pass
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value).replace("```", "'''")[:limit]

    steps = trace.get("steps") or []
    lines = [
        f"REF_ID：{trace.get('ref_id', ref_id)}",
        f"命令：{trace.get('command', '-')}",
        f"触发人：{trace.get('user_id') or '-'}",
        f"触发群：{trace.get('group_id') or '私聊'}",
        f"状态：{trace.get('status', '-')} · 耗时 {trace.get('duration_ms') or 0} ms",
        f"请求摘要：{detail_text(trace.get('input_summary'), 700)}",
    ]
    if trace.get("error_type"):
        lines.append(
            f"错误：{trace.get('error_type')} · {detail_text(trace.get('error_message'), 500)}"
        )
    lines.append(f"请求/返回步骤：{len(steps)} 个")
    for index, step in enumerate(steps[-10:], max(1, len(steps) - 9)):
        lines.append(
            f"{index}. {step.get('step_name', '-')} → {step.get('status', '-')} "
            f"({step.get('duration_ms') or 0} ms)"
        )
        if step.get("detail"):
            lines.append(f"   返回摘要：{detail_text(step.get('detail'), 420)}")
    return _card(
        "REF_ID 查询",
        "\n".join(lines),
        template="green" if trace.get("status") == "success" else "red",
    )


def analysis_tokens_card(report: dict[str, Any]) -> dict:
    content = (
        f"统计范围：近 {report.get('days', 1)} 日\n"
        f"锐评调用：**{_fmt_count(report.get('calls'))}** 次\n"
        f"输入 Token：**{_fmt_count(report.get('input_tokens'))}**\n"
        f"输出 Token：**{_fmt_count(report.get('output_tokens'))}**\n"
        f"总 Token：**{_fmt_count(report.get('total_tokens'))}**\n"
        f"缓存输入：**{_fmt_count(report.get('cached_input_tokens'))}**\n"
        f"有 usage 的调用：{report.get('usage_available_calls', 0)} 次"
    )
    return _card("今日锐评 Token 消耗", content, template="yellow")


def api_report_card(
    report: dict[str, Any], runtime: dict[str, Any] | None = None
) -> dict:
    runtime = runtime or {}
    quota = report.get("quota") or {}
    lines = [
        f"模式：{runtime.get('awmc_api_mode', '-')} · Token 配置："
        f"{'已配置' if runtime.get('awmc_api_token_configured') else '未配置'}",
        f"近 {report.get('days', 1)} 日调用：**{_fmt_count(report.get('calls'))}** 次",
        f"成功：{report.get('success', 0)} · 错误：{report.get('errors', 0)} · "
        f"预期 404（已隐藏）：{report.get('not_found_404', 0)}",
        f"限额触发：{quota.get('exceeded', 0)} 次 · 已观测配额：{quota.get('observed', 0)} 次",
    ]
    latest = quota.get("latest") or {}
    if latest:
        scope = str(latest.get("scope") or "-")
        category = str(latest.get("category") or "-")
        window = str(latest.get("windowLabel") or latest.get("window") or "-")
        used = latest.get("used")
        limit = latest.get("limit")
        usage = f"{used}/{limit}" if used is not None and limit is not None else "未知"
        lines.append(f"最近配额：{scope} · {category} · {window} · 使用 {usage}")
    else:
        lines.append("最近配额：上游尚未返回 used/limit 明细")
    for row in (report.get("paths") or [])[:8]:
        lines.append(
            f"· {str(row.get('path') or '-')[:60]}：{row.get('calls', 0)} 次，"
            f"错误 {row.get('errors', 0)}"
        )
    return _card("AWMC API 调用统计", "\n".join(lines), template="turquoise")


def message_stats_card(rows: list[dict[str, Any]], *, days: int = 1) -> dict:
    """Show rankings only for numeric QQ/group IDs; official encrypted IDs are omitted."""
    window_days = max(1, int(days))
    users: dict[str, int] = {}
    groups: dict[str, int] = {}
    for row in rows:
        user_id = str(row.get("user_id") or "")
        group_id = str(row.get("group_id") or "")
        if not user_id.isdigit() or not group_id.isdigit():
            continue
        messages = max(0, int(row.get("messages") or 0))
        users[user_id] = users.get(user_id, 0) + messages
        groups[group_id] = groups.get(group_id, 0) + messages

    total = sum(users.values())
    average_per_second = total / (window_days * 86400)
    average_per_minute = total / (window_days * 1440)
    lines = [
        f"统计窗口：最近 {window_days} 天",
        f"有效消息总量：**{_fmt_count(total)}** 条",
        f"平均每秒消息：**{average_per_second:.6f}** 条",
        f"平均每分钟消息：**{average_per_minute:.6f}** 条",
        "（未绑定的官方加密 ID 不计入榜单和总量）",
        "",
        "用户 TOP 10",
    ]
    if users:
        lines.extend(
            f"{index}. QQ {user_id}：{_fmt_count(count)} 条"
            for index, (user_id, count) in enumerate(
                sorted(users.items(), key=lambda item: item[1], reverse=True)[:10], 1
            )
        )
    else:
        lines.append("暂无可展示的数字 QQ 数据。")
    lines.extend(["", "群 TOP 10"])
    if groups:
        lines.extend(
            f"{index}. 群 {group_id}：{_fmt_count(count)} 条"
            for index, (group_id, count) in enumerate(
                sorted(groups.items(), key=lambda item: item[1], reverse=True)[:10], 1
            )
        )
    else:
        lines.append("暂无可展示的数字群数据。")
    actions = [
        _button("近 1 日", "messages", days=1),
        _button("近 3 日", "messages", days=3),
        _button("近 7 日", "messages", days=7),
        _button("近 30 日", "messages", days=30),
    ]
    return _card("消息量统计", "\n".join(lines), template="green", actions=actions)


def logs_card(snapshot: LogSnapshot, *, is_admin: bool) -> dict:
    label = "错误日志" if snapshot.errors_only else "运行日志"
    page_lines = snapshot.current_lines()
    body = "\n".join(page_lines) or "没有匹配的日志。"
    body = body[-7000:]
    total = len(snapshot.lines)
    total_pages = snapshot.total_pages()
    # 显示页码：最新一页 = 最后一页，越早页码越小
    page_num = total_pages - snapshot.current_page
    # 窗口标签：「近 2h」「近 10m」等
    ws = snapshot.window_secs
    if ws >= 3600 and ws % 3600 == 0:
        mode_label = f"近 {ws // 3600}h"
    elif ws >= 60 and ws % 60 == 0:
        mode_label = f"近 {ws // 60}m"
    else:
        mode_label = f"近 {ws}s"
    header_line = (
        f"已脱敏 · {mode_label} · 共 {total} 行 · 第 {page_num}/{total_pages} 页"
    )
    actions = [
        _button(
            "刷新",
            "logs_refresh",
            primary=True,
            errors_only=snapshot.errors_only,
            window_secs=snapshot.window_secs,
        ),
        _button(
            "上一页(更早)",
            "logs_prev",
            errors_only=snapshot.errors_only,
        ),
        _button(
            "下一页(更新)",
            "logs_next",
            errors_only=snapshot.errors_only,
        ),
    ]
    # 快捷窗口切换按钮（保留原窗口参数并切换）
    for ws_btn, ws_label in ((3600, "近1h"), (600, "近10m"), (7200, "近2h")):
        if ws_btn != snapshot.window_secs:
            actions.append(
                _button(
                    ws_label,
                    "logs_refresh",
                    errors_only=snapshot.errors_only,
                    window_secs=ws_btn,
                )
            )
    actions.append(_button("运行状态", "status"))
    if is_admin:
        actions.append(_button("管理", "admin_menu"))
    return _card(
        f"AWMC Bot {label}",
        f"{header_line}\n{body}",
        template="red" if snapshot.errors_only and page_lines else "blue",
        actions=actions,
        plain_text=True,
    )


def admin_menu_card(*, is_super_admin: bool = False) -> dict:
    actions = [
        _button("重启 Bot", "control_prepare", operation="restart"),
        _button("启动 Bot", "control_prepare", operation="start"),
        _button("停止 Bot", "control_prepare", operation="stop"),
        _button("运行状态", "status"),
    ]
    if is_super_admin:
        actions.append(_button("权限管理", "permissions"))
    return _card(
        "AWMC Bot 管理操作",
        "Bot 启停操作需要二次确认。BREAK 与封禁操作请使用管理员命令，"
        "机器人会生成带一次性请求 ID 的确认卡片。",
        template="orange",
        actions=actions,
    )


def permissions_card(admins: list[dict[str, Any]], super_admin_ids: frozenset[str]) -> dict:
    lines = ["**当前运维管理员**"]
    for open_id in sorted(super_admin_ids):
        lines.append(f"超级管理员：{open_id}")
    for row in admins:
        lines.append(f"管理员：{row.get('open_id', '-')}")
    if len(lines) == 1:
        lines.append("暂无额外管理员。")
    lines.extend([
        "",
        "授权：授权管理员 <open_id>",
        "撤销：撤销管理员 <open_id>",
    ])
    return _card("飞书权限管理", "\n".join(lines), template="orange")


def bans_card(rows: list[dict[str, Any]], *, all_bans: bool = False) -> dict:
    """渲染封禁列表卡片。最多展示 50 条，超出显示总数。"""
    title = "封禁列表（全部）" if all_bans else "封禁列表（生效中）"
    if not rows:
        return _card(title, "暂无封禁记录。", template="green")
    lines = [f"共 **{len(rows)}** 条记录（展示前 50 条）：", ""]
    for row in rows[:50]:
        user_id = str(row.get("user_id") or "-")
        reason = str(row.get("reason") or "未注明")[:60]
        created = row.get("created_at")
        created_text = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(created)))
            if created else "-"
        )
        expires = row.get("expires_at")
        if expires:
            expire_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(expires)))
        elif all_bans and not row.get("active"):
            expire_text = "已解封/过期"
        else:
            expire_text = "永久"
        actor = str(row.get("actor") or "-")[:30]
        lines.append(
            f"• {user_id}\n  原因：{reason}\n  操作者：{actor}\n"
            f"  生效：{created_text}  到期：{expire_text}"
        )
    return _card(title, "\n".join(lines), template="orange")


def confirmation_card(title: str, summary: str, action: str, **value: Any) -> dict:
    request_id = value.pop("request_id", uuid4().hex)
    return _card(
        title,
        f"{summary}\n\n请求 ID：{request_id[:12]}",
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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ops_admins ("
                "open_id TEXT PRIMARY KEY, granted_by TEXT NOT NULL, "
                "created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1)"
            )

    def is_admin(self, open_id: str) -> bool:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT 1 FROM ops_admins WHERE open_id = ? AND active = 1",
                (str(open_id),),
            ).fetchone()
        return row is not None

    def list_admins(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT open_id, granted_by, created_at FROM ops_admins "
                "WHERE active = 1 ORDER BY created_at"
            ).fetchall()
        return [
            {"open_id": str(row[0]), "granted_by": str(row[1]), "created_at": row[2]}
            for row in rows
        ]

    def grant_admin(self, open_id: str, actor: str) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO ops_admins(open_id, granted_by, created_at, active) "
                "VALUES (?, ?, ?, 1) ON CONFLICT(open_id) DO UPDATE SET "
                "granted_by = excluded.granted_by, created_at = excluded.created_at, active = 1",
                (str(open_id), str(actor), time.time()),
            )

    def revoke_admin(self, open_id: str) -> bool:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "UPDATE ops_admins SET active = 0 WHERE open_id = ? AND active = 1",
                (str(open_id),),
            )
            return cur.rowcount > 0

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

    def logs(
        self,
        *,
        errors_only: bool = False,
        window_secs: int = LOG_WINDOW_DEFAULT_SECS,
    ) -> list[str]:
        """拉取近 window_secs 秒的日志，带行数上限和脱敏。

        - --since 限定窗口起点，-n 上限兜底防 journalctl 超时。
        """
        since_ts = time.time() - window_secs
        since_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts))
        args = [
            "journalctl",
            "-u",
            SERVICE_NAME,
            "-n",
            str(LOG_FETCH_LINES_ALL),
            "--no-pager",
            "-o",
            "short-iso",
            "--since",
            since_str,
        ]
        raw = self._run(args, timeout=LOG_FETCH_TIMEOUT_SECONDS)
        return sanitize_logs(raw.splitlines(), errors_only=errors_only)

    def logs_incremental(
        self,
        *,
        errors_only: bool = False,
        since_ts: str,
    ) -> list[str]:
        """增量拉取：只拉 since_ts 时间点之后的新日志行。

        --since 是闭区间，会重复包含 since_ts 那秒的行，调用方需用
        _line_ts > since_ts 去重。
        """
        args = [
            "journalctl",
            "-u",
            SERVICE_NAME,
            "-n",
            str(LOG_FETCH_LINES_ALL),
            "--no-pager",
            "-o",
            "short-iso",
            "--since",
            since_ts,
        ]
        raw = self._run(args, timeout=LOG_FETCH_TIMEOUT_SECONDS)
        lines = sanitize_logs(raw.splitlines(), errors_only=errors_only)
        return [line for line in lines if _line_ts(line) > since_ts]

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

    def runtime(self) -> dict[str, Any]:
        return self._request("/runtime")

    def command_ranking(self, days: int = 7) -> list[dict[str, Any]]:
        return self._request(f"/commands?days={max(1, min(int(days), 30))}")

    def get_trace(self, ref_id: str) -> dict[str, Any] | None:
        try:
            return self._request(f"/traces/{quote(ref_id.upper(), safe='')}")
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def analysis_tokens(self, days: int = 1) -> dict[str, Any]:
        return self._request(f"/analysis/tokens?days={max(1, min(int(days), 30))}")

    def api_report(self, days: int = 1) -> dict[str, Any]:
        return self._request(f"/api-report?days={max(1, min(int(days), 30))}")

    def message_ranking(self, days: int = 1) -> list[dict[str, Any]]:
        return self._request(f"/messages?days={max(1, min(int(days), 30))}&limit=100")

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

    def list_bans(self, active_only: bool = True) -> list[dict[str, Any]]:
        query = urlencode({"active_only": "true" if active_only else "false"})
        return self._request(f"/bans?{query}")

    def cards_stats(self) -> dict[str, Any]:
        return self._request("/cards/stats")

    def get_card(self, code: str) -> dict[str, Any]:
        return self._request(f"/cards/{quote(code.strip(), safe='')}")

    def create_card(
        self, card_type: str, value: Any, quantity: int, note: str, actor: str
    ) -> dict[str, Any]:
        return self._request(
            "/cards/create",
            method="POST",
            payload={
                "type": card_type,
                "value": value,
                "quantity": quantity,
                "note": note,
                "source": "feishu_ops",
                "actor": actor,
            },
        )

    def disable_card(self, code: str, actor: str) -> dict[str, Any]:
        return self._request(
            f"/cards/{quote(code.strip(), safe='')}/disable",
            method="POST",
            payload={"source": "feishu_ops", "actor": actor},
        )


class FeishuOpsBot:
    def __init__(self, config: OpsConfig, lark_module: Any):
        self.config = config
        self.lark = lark_module
        self.system = SystemController()
        self.admin = AdminClient(config.admin_api_url, config.admin_api_token)
        self.actions = ActionStore(config.state_db)
        # 日志翻页快照：按 (chat_id, errors_only) 隔离。翻页不重新拉 journalctl。
        self._log_snapshots: dict[tuple[str, bool], LogSnapshot] = {}
        self._log_lock = threading.Lock()
        self.client = (
            lark_module.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .log_level(lark_module.LogLevel.WARNING)
            .build()
        )

    def is_admin(self, open_id: str) -> bool:
        return (
            open_id in self.config.admin_open_ids
            or open_id in self.config.super_admin_open_ids
            or self.actions.is_admin(open_id)
        )

    def is_super_admin(self, open_id: str) -> bool:
        return open_id in self.config.super_admin_open_ids

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
            status = self.system.status()
            try:
                status.update(self.admin.runtime())
            except Exception as exc:
                LOG.warning("runtime query failed: %s", type(exc).__name__)
                status["qq_connected"] = None
            return status_card(status, is_admin=is_admin)
        except Exception as exc:
            LOG.exception("status query failed")
            return _card(
                "状态查询失败",
                f"{type(exc).__name__}：{str(exc)[:300]}",
                template="red",
            )

    def _log_key(self, chat_id: str, errors_only: bool) -> tuple[str, bool]:
        # 群聊按群隔离；私聊（菜单事件）chat_id 为空，按 open_id 维度由调用方传入
        return (chat_id or "_private", errors_only)

    def _can_incremental(self, snapshot: Optional[LogSnapshot], window_secs: int) -> bool:
        """判断是否可走增量拉取。条件：有快照、同窗口、未过期、有 last_ts。"""
        if snapshot is None or not snapshot.last_ts:
            return False
        if snapshot.window_secs != window_secs:
            return False
        if time.time() - snapshot.fetched_at > LOG_SNAPSHOT_STALE_SECS:
            return False
        return True

    def _refresh_logs(
        self,
        chat_id: str,
        errors_only: bool,
        window_secs: int,
        is_admin: bool,
    ) -> dict:
        """拉取日志并更新快照。

        优先走「去头填尾」增量：基于上次快照 last_ts 只拉新行，追加到尾部，
        超过 LOG_FETCH_LINES_ALL 时丢弃头部等量旧行，current_page 回到最新页。
        无快照/快照过期/窗口变更/增量失败时走全量 fallback。
        """
        key = self._log_key(chat_id, errors_only)
        with self._log_lock:
            snapshot = self._log_snapshots.get(key)

        # 尝试增量
        if self._can_incremental(snapshot, window_secs):
            try:
                new_lines = self.system.logs_incremental(
                    errors_only=errors_only, since_ts=snapshot.last_ts
                )
            except Exception:
                LOG.exception("incremental log fetch failed, fallback to full")
                new_lines = None
            if new_lines is not None:
                with self._log_lock:
                    snapshot.lines.extend(new_lines)
                    # 超限：丢头部等量旧行（去头填尾）
                    if len(snapshot.lines) > LOG_FETCH_LINES_ALL:
                        snapshot.lines = snapshot.lines[-LOG_FETCH_LINES_ALL:]
                    snapshot.fetched_at = time.time()
                    snapshot.last_ts = (
                        _line_ts(snapshot.lines[-1]) if snapshot.lines else ""
                    )
                    snapshot.current_page = 0
                    current = snapshot
                return logs_card(current, is_admin=is_admin)

        # 全量 fallback
        try:
            lines = self.system.logs(
                errors_only=errors_only, window_secs=window_secs
            )
        except Exception as exc:
            LOG.exception("log query failed")
            # 全量失败保留旧快照（管理员可继续翻页看旧数据）
            if snapshot is not None:
                return logs_card(snapshot, is_admin=is_admin)
            return _card(
                "日志查询失败",
                f"{type(exc).__name__}：{str(exc)[:300]}",
                template="red",
            )
        new_snapshot = LogSnapshot(
            lines=lines,
            errors_only=errors_only,
            fetched_at=time.time(),
            window_secs=window_secs,
            last_ts=_line_ts(lines[-1]) if lines else "",
        )
        with self._log_lock:
            self._log_snapshots[key] = new_snapshot
        return logs_card(new_snapshot, is_admin=is_admin)

    def _paginate_logs(
        self,
        chat_id: str,
        errors_only: bool,
        direction: str,
        is_admin: bool,
    ) -> tuple[dict, bool]:
        """翻页：只移动 cursor，不重新拉 journalctl。无快照时自动刷新一次。

        返回 (卡片, 是否移动成功)；移动失败表示已到边界。
        """
        with self._log_lock:
            snapshot = self._log_snapshots.get(
                self._log_key(chat_id, errors_only)
            )
            if snapshot is not None:
                if direction == "prev":
                    moved = snapshot.go_prev()
                else:
                    moved = snapshot.go_next()
                current = snapshot
        if snapshot is None:
            return (
                self._refresh_logs(
                    chat_id, errors_only, LOG_WINDOW_DEFAULT_SECS, is_admin
                ),
                True,
            )
        return logs_card(current, is_admin=is_admin), moved

    def _safe_business_card(self, kind: str, *, days: int = 1) -> dict:
        try:
            if kind == "overview":
                runtime = self.admin.runtime()
                commands = self.admin.command_ranking(7)
                tokens = self.admin.analysis_tokens(1)
                messages = self.admin.message_ranking(1)
                api_report = self.admin.api_report(1)
                content = (
                    f"**QQ：** {'已连接' if runtime.get('qq_connected') else '等待连接'}\n"
                    f"**适配器：** {', '.join(runtime.get('adapters') or ['-'])}\n"
                    f"**指令调用（7日）：** {sum(int(x.get('calls') or 0) for x in commands)} 次\n"
                    f"**消息量（今日）：** {sum(int(x.get('messages') or 0) for x in messages)} 条\n"
                    f"**锐评 Token（今日）：** {tokens.get('total_tokens', 0)}\n"
                    f"**AWMC API（今日）：** {api_report.get('calls', 0)} 次，"
                    f"错误 {api_report.get('errors', 0)}"
                )
                return _card(
                    "AWMC Bot 业务概览",
                    content,
                    actions=[
                        _button("指令调用", "commands"),
                        _button("消息量", "messages", days=1),
                        _button("锐评 Token", "analysis_tokens"),
                        _button("AWMC API", "api_report"),
                    ],
                )
            if kind == "commands":
                return command_stats_card(self.admin.command_ranking(7))
            if kind == "analysis_tokens":
                return analysis_tokens_card(self.admin.analysis_tokens(1))
            if kind == "api_report":
                return api_report_card(self.admin.api_report(1), self.admin.runtime())
            if kind == "messages":
                selected_days = days if days in {1, 3, 7, 30} else 1
                return message_stats_card(
                    self.admin.message_ranking(selected_days), days=selected_days
                )
            if kind == "ref_query":
                return _card(
                    "REF_ID 查询", "请使用 查询REF <REF_ID>。", template="orange"
                )
        except Exception as exc:
            LOG.exception("business query failed: %s", kind)
            return _card(
                "业务查询失败",
                f"查询 {kind} 时发生 {type(exc).__name__}：{str(exc)[:300]}",
                template="red",
            )
        return menu_card(is_admin=True)

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
            card = self._dispatch_command(command, args, open_id, is_admin, chat_id)
        except Exception as exc:
            if isinstance(exc, ValueError):
                LOG.info("command usage rejected: %s", command)
                card = _card("命令参数不正确", str(exc)[:500], template="orange")
            else:
                LOG.exception("command failed: %s", command)
                card = _card(
                    "操作失败", f"{type(exc).__name__}：{str(exc)[:300]}", template="red"
                )
        if card is not None:
            self._reply_card(message.message_id, card)

    def _dispatch_command(
        self,
        command: str,
        args: list[str],
        open_id: str,
        is_admin: bool,
        chat_id: str = "",
    ) -> dict:
        if command == "menu":
            return menu_card(
                is_admin=is_admin, is_super_admin=self.is_super_admin(open_id)
            )
        if command == "identity":
            return _card("飞书身份", f"open_id：{open_id}")
        if command == "status":
            return self._safe_status_card(is_admin)
        if command in {"logs", "errors"}:
            # 解析时间窗口参数：「日志 2h」「错误 10m」「日志 1h」等
            # 无参数则默认 LOG_WINDOW_DEFAULT_SECS（近 10 分钟）
            window_secs = LOG_WINDOW_DEFAULT_SECS
            if args:
                parsed = parse_window_secs(args[0])
                if parsed is not None:
                    window_secs = parsed
            return self._refresh_logs(
                chat_id, command == "errors", window_secs, is_admin
            )
        is_super_admin = self.is_super_admin(open_id)
        if command == "permissions":
            if not is_super_admin:
                return _card("权限不足", "权限管理仅限超级管理员。", template="red")
            return permissions_card(
                self.actions.list_admins(), self.config.super_admin_open_ids
            )
        if command in {"grant_admin", "revoke_admin"}:
            if not is_super_admin:
                return _card("权限不足", "授权管理员仅限超级管理员。", template="red")
            if len(args) != 1 or not re.fullmatch(r"(?:ou|on)_[A-Za-z0-9_-]{6,128}", args[0]):
                raise ValueError(
                    "用法：授权管理员 <飞书 open_id> 或 撤销管理员 <飞书 open_id>"
                )
            grant = command == "grant_admin"
            return confirmation_card(
                f"确认{'授权' if grant else '撤销'}管理员",
                f"目标 open_id：{args[0]}",
                "admin_grant_confirm" if grant else "admin_revoke_confirm",
                target_open_id=args[0],
            )
        if not is_admin:
            return _card("权限不足", "该操作仅限飞书运维管理员。", template="red")
        if command in {
            "overview",
            "commands",
            "analysis_tokens",
            "messages",
            "api_report",
        }:
            days = 1
            if command == "messages" and args:
                if len(args) != 1 or not args[0].isdigit() or int(args[0]) not in {1, 3, 7, 30}:
                    raise ValueError("用法：消息量 [1|3|7|30]")
                days = int(args[0])
            return self._safe_business_card(command, days=days)
        if command == "ref_query":
            if len(args) != 1 or not re.fullmatch(
                r"REF-[A-Z0-9]{8,32}", args[0].upper()
            ):
                raise ValueError("用法：查询REF <REF-十六位编号>")
            return ref_card(self.admin.get_trace(args[0]), args[0].upper())
        if command == "admin":
            return admin_menu_card(is_super_admin=is_super_admin)
        if command in {"start", "stop", "restart"}:
            labels = {"start": "启动", "stop": "停止", "restart": "重启"}
            return confirmation_card(
                f"确认{labels[command]} Bot",
                f"即将对 {SERVICE_NAME} 执行 {command}。",
                "control_confirm",
                operation=command,
            )
        if command == "break_get":
            if len(args) != 1 or not args[0].isdigit():
                raise ValueError(
                    "用法：查询BREAK QQ号\n示例：查询BREAK 123456789"
                )
            row = self.admin.get_user(args[0])
            if row is None:
                return _card(
                    "BREAK 查询", f"未找到用户 {args[0]}。", template="orange"
                )
            return _card(
                "BREAK 查询",
                f"用户：{args[0]}\n余额：**{row.get('break', 0)} BREAK**\n"
                f"封禁：{bool(row.get('banned'))}",
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
                f"目标用户：{args[0]}\n{verb}：**{amount} BREAK**",
                "break_confirm",
                user_id=args[0],
                amount=amount,
                mode=mode,
            )
        if command == "card_stats":
            data = self.admin.cards_stats()
            lines = [f"共 **{data.get('total', 0)}** 张，生效中加成 **{data.get('active_effects', 0)}** 人"]
            for ctype in ("break", "double_break", "freedom"):
                info = data.get("by_type", {}).get(ctype, {})
                lines.append(
                    f"{CARD_TYPE_LABELS[ctype]}：未使用 {info.get('unused', 0)} / "
                    f"已兑换 {info.get('redeemed', 0)} / 已作废 {info.get('disabled', 0)}"
                )
            return _card("卡密统计", "\n".join(lines))
        if command == "card_get":
            if len(args) != 1:
                raise ValueError("用法：查询卡密 <卡密或批次号>")
            try:
                data = self.admin.get_card(args[0])
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    return _card("卡密查询", "未找到该卡密或批次。", template="orange")
                raise
            if "cards" in data and "code" not in data:
                cards = data["cards"][:50]
                lines = [f"批次 {data['batch']} 共 {len(cards)} 张（最多 50）"]
                for c in cards:
                    status = {"unused": "未使用", "redeemed": "已兑换",
                              "disabled": "已作废"}.get(c.get("status"), c.get("status"))
                    lines.append(f"{c.get('code')} · {CARD_TYPE_LABELS.get(c.get('card_type'), c.get('card_type'))} · {status}")
                return _card("批次查询", "\n".join(lines))
            ctype = data.get("card_type")
            if ctype == "break":
                value_text = f"{data.get('value')} BREAK"
            else:
                value_text = format_duration(int(data.get("value") or 0))
            status = {"unused": "未使用", "redeemed": "已兑换",
                      "disabled": "已作废"}.get(data.get("status"), data.get("status"))
            lines = [
                f"卡密：{data.get('code')}",
                f"类型：{CARD_TYPE_LABELS.get(ctype, ctype)}",
                f"面值：{value_text}",
                f"状态：{status}",
                f"批次：{data.get('batch_id')}",
                f"创建者：{data.get('created_by') or '-'}",
            ]
            if data.get("status") == "redeemed":
                lines.append(f"兑换者：{data.get('redeemed_by')}")
                lines.append(f"兑换时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data.get('redeemed_at') or 0))}")
            return _card("卡密查询", "\n".join(lines))
        if command == "card_create":
            if len(args) < 2:
                raise ValueError(
                    "用法：创建卡密 <类型> <面值/时长> [数量] [备注]\n"
                    "类型：1=BREAK卡 2=双倍BREAK卡 3=FREEDOM卡\n"
                    "示例：创建卡密 2 24h 5 猜歌活动"
                )
            ctype = resolve_card_type(args[0])
            if not ctype:
                raise ValueError("卡密类型无效：1/2/3 或 break/double/freedom")
            if ctype == "break":
                if not args[1].isdigit() or int(args[1]) <= 0:
                    raise ValueError("BREAK 卡面值必须是正整数")
                value = int(args[1])
                value_text = f"{value} BREAK"
            else:
                value = parse_card_duration(args[1])
                value_text = format_duration(value)
            quantity = 1
            note = ""
            rest = args[2:]
            if rest and rest[0].isdigit():
                quantity = int(rest[0])
                rest = rest[1:]
            if not 1 <= quantity <= 500:
                raise ValueError("数量需在 1～500 之间")
            if rest:
                note = " ".join(rest)[:200]
            return confirmation_card(
                "确认创建卡密",
                f"类型：**{CARD_TYPE_LABELS[ctype]}**\n面值：{value_text}\n"
                f"数量：**{quantity}** 张\n备注：{note or '-'}",
                "card_create_confirm",
                card_type=ctype, value=value, quantity=quantity, note=note,
            )
        if command == "card_disable":
            if len(args) != 1:
                raise ValueError("用法：作废卡密 <卡密>")
            return confirmation_card(
                "确认作废卡密",
                f"即将作废卡密：{args[0]}\n已兑换的卡密无法作废。",
                "card_disable_confirm",
                code=args[0],
            )
        if command == "ban":
            if len(args) < 2:
                raise ValueError(
                    "用法：封禁 QQ号或官方 open_id + 小时 + 原因\n"
                    "示例：封禁 123456789 24 接口滥用"
                )
            hours = float(args[1])
            if not 0 <= hours <= 24 * 365:
                raise ValueError("封禁时长范围为 0～8760 小时")
            reason = " ".join(args[2:]).strip() or "飞书管理员封禁"
            return confirmation_card(
                "确认封禁用户",
                f"用户：{args[0]}\n时长：{'永久' if hours == 0 else f'{hours:g} 小时'}\n"
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
                f"用户：{args[0]}",
                "unban_confirm",
                user_id=args[0],
            )
        if command == "ban_list":
            all_bans = bool(args) and args[0] in {"全部", "all", "历史"}
            rows = self.admin.list_bans(active_only=not all_bans)
            return bans_card(rows, all_bans=all_bans)
        return menu_card(
            is_admin=is_admin, is_super_admin=self.is_super_admin(open_id)
        )

    def handle_card_action(self, data: Any) -> dict:
        event = data.event
        open_id = str(event.operator.open_id or "")
        chat_id = str(event.context.open_chat_id or "")
        value = dict(event.action.value or {})
        action = str(value.get("action") or "")
        is_admin = self.is_admin(open_id)
        if not self.is_allowed_chat(chat_id) and not is_admin:
            return self._response(toast="该群未获授权")
        if action == "menu":
            return self._response(
                menu_card(
                    is_admin=is_admin, is_super_admin=self.is_super_admin(open_id)
                ),
                "已返回主菜单",
            )
        if action == "status":
            return self._response(self._safe_status_card(is_admin), "状态已刷新")
        if action in {"logs", "errors", "logs_refresh", "logs_prev", "logs_next"}:
            errors_only = action == "errors" or bool(value.get("errors_only"))
            if action in {"logs", "errors", "logs_refresh"}:
                # 窗口参数：从按钮 value 取，缺省用默认（近 10 分钟）
                raw_window = value.get("window_secs")
                try:
                    window_secs = int(raw_window) if raw_window is not None else LOG_WINDOW_DEFAULT_SECS
                except (TypeError, ValueError):
                    window_secs = LOG_WINDOW_DEFAULT_SECS
                # 同步拉取（增量优先「去头填尾」，否则全量），原卡片就地更新，不新发卡片
                card = self._refresh_logs(
                    chat_id, errors_only, window_secs, is_admin
                )
                return self._response(card, "已刷新")
            # 翻页：不重新拉 journalctl，只移动快照 cursor
            direction = "prev" if action == "logs_prev" else "next"
            card, moved = self._paginate_logs(
                chat_id, errors_only, direction, is_admin
            )
            if not moved:
                toast = "已是最早一页" if direction == "prev" else "已是最新一页"
                return self._response(card, toast)
            return self._response(card)
        if not is_admin:
            return self._response(toast="该操作仅限管理员")
        if action == "permissions":
            if not self.is_super_admin(open_id):
                return self._response(toast="该操作仅限超级管理员")
            return self._response(
                permissions_card(self.actions.list_admins(), self.config.super_admin_open_ids)
            )
        if action in {
            "overview",
            "commands",
            "analysis_tokens",
            "messages",
            "api_report",
        }:
            days = int(value.get("days") or 1)
            return self._response(
                self._safe_business_card(action, days=days), "查询已刷新"
            )
        if action == "admin_menu":
            return self._response(
                admin_menu_card(is_super_admin=self.is_super_admin(open_id))
            )
        if action == "control_prepare":
            operation = str(value.get("operation") or "")
            labels = {"start": "启动", "stop": "停止", "restart": "重启"}
            if operation not in labels:
                return self._response(toast="未知操作")
            return self._response(
                confirmation_card(
                    f"确认{labels[operation]} Bot",
                    f"即将对 {SERVICE_NAME} 执行 {operation}。",
                    "control_confirm",
                    operation=operation,
                )
            )
        if action == "control_confirm":
            return self._handle_control(value, open_id, chat_id)
        if action in {"admin_grant_confirm", "admin_revoke_confirm"}:
            return self._handle_admin_permission(
                value, open_id, grant=action == "admin_grant_confirm"
            )
        if action == "break_confirm":
            return self._handle_break(value, open_id)
        if action == "card_create_confirm":
            return self._handle_card_create(value, open_id)
        if action == "card_disable_confirm":
            return self._handle_card_disable(value, open_id)
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
                    f"{type(exc).__name__}：{str(exc)[:300]}",
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
            _card("操作已提交", f"正在执行 {operation}，结果会发送到当前群。"),
            "操作已提交",
        )

    def _handle_admin_permission(self, value: dict, actor: str, *, grant: bool) -> dict:
        if not self.is_super_admin(actor):
            return self._response(toast="该操作仅限超级管理员")
        target = str(value.get("target_open_id") or "").strip()
        if not re.fullmatch(r"(?:ou|on)_[A-Za-z0-9_-]{6,128}", target):
            return self._response(toast="open_id 格式无效")
        action = "admin.grant" if grant else "admin.revoke"
        if not self._claim(value, action, actor):
            return self._response(toast="该请求已经执行或已失效")
        if grant:
            self.actions.grant_admin(target, actor)
            message = f"已授权 {target} 为运维管理员。"
        else:
            changed = self.actions.revoke_admin(target)
            message = f"已撤销 {target} 的运维管理员权限。" if changed else "该用户没有额外管理员权限。"
        return self._response(
            permissions_card(self.actions.list_admins(), self.config.super_admin_open_ids),
            message,
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
                f"用户：{user_id}\n当前余额：**{result['balance']} BREAK**\n"
                f"REF_ID：{result.get('ref_id', '-')}",
                template="green",
            ),
            "BREAK 已更新",
        )

    def _handle_card_create(self, value: dict, actor: str) -> dict:
        if not self._claim(value, "card.create", actor):
            return self._response(toast="该请求已经执行或已失效")
        ctype = str(value.get("card_type") or "")
        if ctype not in CARD_TYPE_LABELS:
            return self._response(toast="卡密类型无效")
        try:
            raw_value = value.get("value")
            card_value = int(raw_value) if ctype == "break" else parse_card_duration(str(raw_value))
            quantity = int(value.get("quantity") or 1)
            note = str(value.get("note") or "")[:200]
            result = self.admin.create_card(ctype, card_value, quantity, note, actor)
        except (TypeError, ValueError) as exc:
            return self._response(toast=f"参数无效：{exc}")
        except RuntimeError as exc:
            return self._response(
                _card("创建卡密失败", str(exc)[:300], template="red"), "创建失败"
            )
        value_text = f"{card_value} BREAK" if ctype == "break" else format_duration(card_value)
        codes = result.get("codes") or []
        body = (
            f"类型：{CARD_TYPE_LABELS[ctype]}\n面值：{value_text}\n"
            f"数量：{result.get('quantity', quantity)} 张\n"
            f"批次：{result.get('batch_id')}\nREF_ID：{result.get('ref_id', '-')}\n\n"
            + "\n".join(codes[:50])
            + ("\n..." if len(codes) > 50 else "")
        )
        return self._response(
            _card("卡密创建完成", body, template="green"), "卡密已创建"
        )

    def _handle_card_disable(self, value: dict, actor: str) -> dict:
        if not self._claim(value, "card.disable", actor):
            return self._response(toast="该请求已经执行或已失效")
        code = str(value.get("code") or "").strip()
        if not code:
            return self._response(toast="卡密不能为空")
        try:
            result = self.admin.disable_card(code, actor)
        except RuntimeError as exc:
            return self._response(
                _card("作废卡密失败", str(exc)[:300], template="red"), "作废失败"
            )
        return self._response(
            _card(
                "卡密已作废",
                f"卡密：{result.get('code')}\nREF_ID：{result.get('ref_id', '-')}",
                template="green",
            ),
            "已作废",
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
                f"用户：{user_id}\nREF_ID：{result.get('ref_id', '-')}",
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
        # 菜单事件是私聊推送，按 open_id 维度隔离日志快照
        cards = {
            "ops_status": lambda: self._safe_status_card(True),
            "ops_logs": lambda: self._refresh_logs(
                open_id, False, LOG_WINDOW_DEFAULT_SECS, True
            ),
            "ops_errors": lambda: self._refresh_logs(
                open_id, True, LOG_WINDOW_DEFAULT_SECS, True
            ),
            "ops_admin": lambda: admin_menu_card(
                is_super_admin=self.is_super_admin(open_id)
            ),
            "ops_overview": lambda: self._safe_business_card("overview"),
            "ops_commands": lambda: self._safe_business_card("commands"),
            "ops_messages": lambda: self._safe_business_card("messages"),
            "ops_analysis_tokens": lambda: self._safe_business_card("analysis_tokens"),
            "ops_api_report": lambda: self._safe_business_card("api_report"),
        }
        factory = cards.get(key)
        if factory:
            card = factory()
            if card is not None:
                self._send_card("open_id", open_id, card)


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

    def ignore_reaction_event(_data: Any) -> None:
        # The app has this unrelated subscription; do not log reaction contents.
        return None

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card)
        .register_p2_application_bot_menu_v6(on_menu)
        .register_p2_customized_event(
            "im.message.reaction.created_v1", ignore_reaction_event
        )
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
