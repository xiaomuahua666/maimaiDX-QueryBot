"""持久化记录音频/谱面预渲染任务。

任务本身仍由各自的渲染器执行；本模块只保存可恢复参数、进度和 ETA，
这样进程重启后可以重新排队，也让管理员能一次看到所有渲染任务。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = _ROOT / "data" / "render_tasks.json"
_LOCK = RLock()


def _read() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(TASK_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(data: dict[str, dict[str, Any]]) -> None:
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="render_tasks.", suffix=".json", dir=TASK_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, TASK_FILE)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def mark_interrupted_tasks_pending() -> int:
    """把上次进程被打断的 running 任务改成 pending，返回数量。"""
    with _LOCK:
        data = _read()
        changed = 0
        for task in data.values():
            if task.get("status") == "running":
                task["status"] = "pending"
                task["message"] = "等待 Bot 启动后自动恢复"
                task["updated_at"] = time.time()
                changed += 1
        if changed:
            _write(data)
        return changed


def start_task(
    kind: str,
    *,
    total: int,
    force: bool = False,
    limit: Optional[int] = None,
    task_id: Optional[str] = None,
) -> str:
    """登记一个任务；同一 kind 只保留一个活动任务。"""
    now = time.time()
    task_id = task_id or f"{kind}-{int(now * 1000)}"
    task = {
        "id": task_id,
        "kind": kind,
        "status": "running",
        "total": max(0, int(total)),
        "processed": 0,
        "force": bool(force),
        "limit": limit,
        "started_at": now,
        "updated_at": now,
        "eta_seconds": None,
        "message": "任务已开始",
    }
    with _LOCK:
        data = _read()
        for key in list(data):
            if data[key].get("kind") == kind and data[key].get("status") in {"running", "pending"}:
                del data[key]
        data[task_id] = task
        _write(data)
    return task_id


def update_task(
    task_id: str,
    *,
    processed: Optional[int] = None,
    total: Optional[int] = None,
    eta_seconds: Optional[float] = None,
    message: Optional[str] = None,
) -> None:
    with _LOCK:
        data = _read()
        task = data.get(task_id)
        if not task:
            return
        if processed is not None:
            task["processed"] = max(0, int(processed))
        if total is not None:
            task["total"] = max(0, int(total))
        if eta_seconds is not None:
            task["eta_seconds"] = max(0.0, float(eta_seconds))
        if message is not None:
            task["message"] = str(message)
        task["updated_at"] = time.time()
        _write(data)


def finish_task(task_id: str, *, status: str = "done", message: str = "") -> None:
    with _LOCK:
        data = _read()
        task = data.get(task_id)
        if not task:
            return
        # Completed tasks do not belong in the active-task query; failed tasks
        # stay briefly visible for diagnostics but are never auto-resumed.
        if status in {"done", "cancelled"}:
            del data[task_id]
        else:
            task["status"] = status
            task["message"] = message
            task["updated_at"] = time.time()
        _write(data)


def active_tasks() -> list[dict[str, Any]]:
    with _LOCK:
        data = _read()
        return [
            dict(task)
            for task in data.values()
            if task.get("status") in {"running", "pending"}
        ]


def pending_tasks() -> list[dict[str, Any]]:
    with _LOCK:
        data = _read()
        return [dict(task) for task in data.values() if task.get("status") == "pending"]


def format_active_tasks() -> str:
    tasks = active_tasks()
    if not tasks:
        return "当前没有正在进行中的渲染。"
    lines = [f"当前渲染任务（{len(tasks)} 个）："]
    labels = {"chart": "谱面", "audio": "音频"}
    for task in sorted(tasks, key=lambda item: float(item.get("started_at") or 0)):
        total = int(task.get("total") or 0)
        done = int(task.get("processed") or 0)
        eta = task.get("eta_seconds")
        eta_text = "计算中" if eta is None else _format_duration(float(eta))
        state = "等待恢复" if task.get("status") == "pending" else "进行中"
        lines.append(
            f"- {labels.get(task.get('kind'), task.get('kind', 'render'))}："
            f"{state} {done}/{total}，剩余 {eta_text}"
        )
        if task.get("message"):
            lines.append(f"  {task['message']}")
    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, value = divmod(value, 3600)
    minutes, secs = divmod(value, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    return f"{minutes}分{secs:02d}秒"


# Import-time recovery marker: a hard process stop never gets a finally block.
mark_interrupted_tasks_pending()
