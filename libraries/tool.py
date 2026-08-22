import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Union

import aiofiles
from playwright.async_api import async_playwright

from ..config import SNAPSHOT_JS, pie_html_file


def qqhash(qq: int):
    days = int(time.strftime("%d", time.localtime(time.time()))) + 31 * int(
        time.strftime("%m", time.localtime(time.time()))) + 77
    return (days * qq) >> 8


async def openfile(file: Path) -> Union[dict, list]:
    async with aiofiles.open(file, 'r', encoding='utf-8') as f:
        data = json.loads(await f.read())
    return data


def is_cache_fresh(file: Path, ttl_seconds: int) -> bool:
    """
    判断本地缓存文件是否在有效期内。

    Args:
        file: 缓存文件路径
        ttl_seconds: 有效期（秒）。<= 0 时始终视为过期（禁用缓存）
    Returns:
        True 表示文件存在且未过期，可直接使用本地缓存
    """
    if ttl_seconds <= 0:
        return False
    try:
        if not file.exists():
            return False
        return (time.time() - file.stat().st_mtime) < ttl_seconds
    except OSError:
        return False


async def writefile(file: Path, data: Any) -> bool:
    # 原子写：先写临时文件再 rename。调用方（猜歌积分、开信统计等）会把
    # 整份存档反复全量重写，直接覆盖写一旦进程中途崩溃就是整档损坏。
    tmp = file.with_name(file.name + '.tmp')
    async with aiofiles.open(tmp, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=4))
    os.replace(tmp, file)
    return True


async def run_chrome_to_base64() -> str:
    async with async_playwright() as p:
        browers = await p.chromium.launch(headless=True)
        page = await browers.new_page(java_script_enabled=True)
        await page.goto('file://' + str(pie_html_file))
        await asyncio.sleep(2)
        
        content: str = await page.evaluate(SNAPSHOT_JS)
        await browers.close()
        
    content_array = content.split(',')
    if len(content_array) != 2:
        raise OSError(content_array)

    return 'base64://' + content_array[-1]