"""图片合成专用线程池，避免 Pillow 阻塞 NoneBot 事件循环。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import os
from typing import Any, Callable

from loguru import logger as log


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


_CPU_COUNT = max(1, int(os.cpu_count() or 4))
# Pillow/封面合成独立于事件循环；默认按半数 CPU 并发并设置上限，
# 让多局小游戏绘图有余量，同时避免无限制线程把机器打满。
IMAGE_WORKERS = _env_int(
    'MAIMAIDX_IMAGE_WORKERS', min(12, max(4, _CPU_COUNT // 2)),
)
_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=IMAGE_WORKERS,
            thread_name_prefix='maimaidx-image',
        )
        log.info(
            f'[ImageRender] 专用图片线程池 workers={IMAGE_WORKERS} cpu={_CPU_COUNT}'
        )
    return _EXECUTOR


async def run_image_cpu(func: Callable[..., Any], /, *args, **kwargs):
    """在线程池中执行 Pillow 合成，保持消息与网络事件循环响应。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor(), functools.partial(func, *args, **kwargs),
    )
