"""共享磁盘/JSON I/O 线程池。

SQLite、JSON 索引和批量审计写入都属于短时阻塞 I/O。它们不能跑在
NoneBot 事件循环里，否则补存扫描 795 个用户时会卡住所有消息分发。
这里提供一个有界线程池，让缓存、快照和审计热路径都复用它。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from typing import Any, Callable, TypeVar


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


_IO_WORKERS = _env_int("MAIMAIDX_IO_WORKERS", 12)
_EXECUTOR = ThreadPoolExecutor(
    max_workers=_IO_WORKERS,
    thread_name_prefix="maimaidx-io",
)

T = TypeVar("T")


def run_io(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking filesystem/SQLite operation off the event loop."""
    return _EXECUTOR.submit(fn, *args, **kwargs).result()


async def run_io_async(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Await the same blocking operation from an async context."""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args, **kwargs))
