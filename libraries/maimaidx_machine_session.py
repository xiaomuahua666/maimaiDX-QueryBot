"""Serialize workflows that share the configured arcade machine identity."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from ..config import log, maiconfig


# One configured keychip represents one arcade machine session.  Concurrent
# logins can invalidate one another even when they belong to different users,
# so this lock is intentionally global rather than keyed by QQ/user ID.
_machine_lock = asyncio.Lock()


class MachineBusyError(RuntimeError):
    """机台会话被其他任务占用，等待获取锁超时。"""


@asynccontextmanager
async def machine_session() -> AsyncIterator[None]:
    """Hold the shared machine session for one complete business workflow.

    等待超时由 ``AWMC_MACHINE_LOCK_TIMEOUT_SECONDS`` 控制（默认 120s）；
    设为 ``0`` 表示无限等待（旧行为）。
    """
    timeout = float(getattr(maiconfig, "awmc_machine_lock_timeout_seconds", 120.0) or 0.0)
    if timeout > 0:
        if _machine_lock.locked():
            log.info(f"[machine_session] 机台锁被占用，最多等待 {timeout:.0f}s")
        try:
            await asyncio.wait_for(_machine_lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MachineBusyError(
                f"机台繁忙（等待超过 {timeout:.0f} 秒），请稍后重试"
            ) from exc
    else:
        if _machine_lock.locked():
            log.info("[machine_session] 机台锁被占用，无限等待中")
        await _machine_lock.acquire()
    try:
        yield
    finally:
        _machine_lock.release()
