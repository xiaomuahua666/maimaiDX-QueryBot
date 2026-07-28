"""机台二维码 SGWCMAID 提取与日志脱敏。"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
from io import BytesIO
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

import httpx
import numpy as np
from PIL import Image, ImageFilter, ImageOps

# 机台二维码：SGWCMAID 后接连续非空白字符（可嵌在「mai绑定 SGWCMAID…」等文本中）
SGWCMAID_PATTERN = re.compile(r'(SGWCMAID\S+)')

# 舞萌二维码页面的两种官方链接。路径里的 MAID… 与 SGWCMAID… 仅差 SGWC 前缀。
WAHLAP_QR_URL_PATTERN = re.compile(
    r"https?://wq\.wahlap\.net/qrcode/"
    r"(?:(?:img/(?P<img>MAID[A-Z0-9]{20,160})\.png)"
    r"|(?:req/(?P<req>MAID[A-Z0-9]{20,160})\.html))"
    r"(?=[?#\s]|$)",
    re.IGNORECASE,
)

# 直发监听只接管消息开头的二维码凭据或官方二维码链接。
DIRECT_QRCODE_PREFIX_PATTERN = (
    r"^\s*(?:SGWCMAID|https?://wq\.wahlap\.net/qrcode/(?:img|req)/MAID)"
)

_QRCODE_QUICK_CHECK = 'SGWCMAID'
_IMAGE_MAX_PIXELS = 25_000_000
_IMAGE_QR_MIN_LONG_SIDE = 900
_IMAGE_QR_SCAN_TIMEOUT_SECONDS = 4.0
_IMAGE_QR_SCAN_QUEUE_SECONDS = 0.05
_IMAGE_QR_SCAN_SEMAPHORE = asyncio.Semaphore(1)


def message_may_contain_qrcode(text: str) -> bool:
    """快速预判，避免对每条群消息做正则。"""
    return _QRCODE_QUICK_CHECK in text or bool(WAHLAP_QR_URL_PATTERN.search(text))


def extract_sgwcmaid_qrcode(text: str) -> Optional[str]:
    """提取 SGWCMAID，兼容官方 img/req 二维码链接。"""
    if not text:
        return None
    if _QRCODE_QUICK_CHECK in text:
        match = SGWCMAID_PATTERN.search(text)
        if match:
            return match.group(1)
    link_match = WAHLAP_QR_URL_PATTERN.search(text)
    if not link_match:
        return None
    maid = link_match.group("img") or link_match.group("req")
    return "SGWC" + maid.upper()


def qrcode_log_preview(qrcode: str, *, head: int = 24, tail: int = 8) -> str:
    """日志用脱敏预览，避免完整泄露二维码。"""
    if len(qrcode) <= head + tail + 3:
        return f'{qrcode[:head]}…'
    return f'{qrcode[:head]}…{qrcode[-tail:]}'


def decode_sgwcmaid_qrcode_image(image_bytes: bytes) -> Optional[str]:
    """识别图片中的二维码，仅返回有效 SGWCMAID/舞萌官方二维码链接。"""
    if not image_bytes:
        return None
    import zxingcpp

    with Image.open(BytesIO(image_bytes)) as opened:
        width, height = opened.size
        if width <= 0 or height <= 0:
            return None
        image = ImageOps.exif_transpose(opened).convert('RGB')
        if width * height > _IMAGE_MAX_PIXELS:
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)

    for candidate in _qrcode_scan_candidates(image):
        pixels = np.asarray(candidate)
        for binarizer in (
            zxingcpp.Binarizer.LocalAverage,
            zxingcpp.Binarizer.GlobalHistogram,
            zxingcpp.Binarizer.FixedThreshold,
        ):
            try:
                results = zxingcpp.read_barcodes(
                    pixels,
                    formats=zxingcpp.BarcodeFormat.QRCode,
                    binarizer=binarizer,
                    try_invert=True,
                    try_downscale=True,
                    try_rotate=True,
                )
            except Exception:
                continue
            for result in results:
                qrcode = extract_sgwcmaid_qrcode(str(result.text or '').strip())
                if qrcode:
                    return qrcode
    return None


def _qrcode_scan_candidates(image: Image.Image) -> list[Image.Image]:
    """按识别友好度依次返回候选图像：原图 → 放大 → 灰度增强 → 锐化。"""
    candidates: list[Image.Image] = [image]

    long_side = max(image.size)
    if 0 < long_side < _IMAGE_QR_MIN_LONG_SIDE:
        scale = _IMAGE_QR_MIN_LONG_SIDE / long_side
        new_size = (
            max(1, int(round(image.width * scale))),
            max(1, int(round(image.height * scale))),
        )
        candidates.append(image.resize(new_size, Image.Resampling.LANCZOS))

    gray = ImageOps.autocontrast(image.convert('L'), cutoff=2)
    candidates.append(gray)
    candidates.append(gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=180)))
    return candidates


async def _decode_sgwcmaid_qrcode_image_isolated(
    image_bytes: bytes,
) -> Optional[str]:
    """在可强制结束的子进程中扫码，避免 native 解码器长期持有 GIL 卡死 Bot。"""
    try:
        await asyncio.wait_for(
            _IMAGE_QR_SCAN_SEMAPHORE.acquire(),
            timeout=_IMAGE_QR_SCAN_QUEUE_SECONDS,
        )
    except asyncio.TimeoutError:
        # 普通群图很多时直接跳过排队，避免扫码任务反过来堆积消息处理。
        return None

    process: Optional[asyncio.subprocess.Process] = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve()),
            '--decode-stdin',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(image_bytes),
            timeout=_IMAGE_QR_SCAN_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, OSError):
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        return None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        _IMAGE_QR_SCAN_SEMAPHORE.release()

    if process.returncode != 0 or not stdout:
        return None
    return extract_sgwcmaid_qrcode(stdout.decode('utf-8', errors='ignore').strip())


async def _read_local_image(path: Path, max_bytes: int) -> Optional[bytes]:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        return await asyncio.to_thread(path.read_bytes)
    except OSError:
        return None


async def _read_remote_image(
    client: httpx.AsyncClient, url: str, max_bytes: int
) -> Optional[bytes]:
    if not _safe_remote_image_url(url):
        return None
    chunks: list[bytes] = []
    total = 0
    async with client.stream('GET', url) as response:
        response.raise_for_status()
        if not _safe_remote_image_url(str(response.url)):
            return None
        content_type = response.headers.get('content-type', '').lower()
        if content_type and not (
            content_type.startswith('image/')
            or content_type.startswith('application/octet-stream')
        ):
            return None
        declared = response.headers.get('content-length')
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return None
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
    return b''.join(chunks)


def _safe_remote_image_url(url: str) -> bool:
    """拒绝明显的本机/内网 URL，避免图片段被滥用于访问内部服务。"""
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return False
    host = parsed.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith('.localhost'):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


async def _image_segment_bytes(
    segment: Any,
    client: httpx.AsyncClient,
    max_bytes: int,
) -> Optional[bytes]:
    data = getattr(segment, 'data', None) or {}
    raw = str(data.get('url') or data.get('file') or '').strip()
    if not raw:
        return None
    if raw.startswith('base64://'):
        try:
            value = base64.b64decode(raw[9:], validate=True)
        except (ValueError, TypeError):
            return None
        return value if len(value) <= max_bytes else None
    if raw.startswith('data:image/') and ';base64,' in raw:
        try:
            value = base64.b64decode(raw.split(';base64,', 1)[1], validate=True)
        except (ValueError, TypeError):
            return None
        return value if len(value) <= max_bytes else None
    if raw.startswith('file://'):
        return await _read_local_image(Path(raw[7:]), max_bytes)
    local = Path(raw)
    if local.is_absolute():
        return await _read_local_image(local, max_bytes)
    if raw.startswith(('https://', 'http://')):
        return await _read_remote_image(client, raw, max_bytes)
    return None


async def extract_sgwcmaid_from_image_segments(
    segments: Iterable[Any],
    *,
    max_images: int = 3,
    max_bytes: int = 8 * 1024 * 1024,
) -> Optional[str]:
    """依次下载/读取消息图片并扫码；普通图片或非舞萌二维码返回 ``None``。"""
    images = [
        segment
        for segment in segments
        if getattr(segment, 'type', None) == 'image'
    ][:max(1, max_images)]
    if not images:
        return None
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for segment in images:
            try:
                image_bytes = await _image_segment_bytes(segment, client, max_bytes)
            except (httpx.HTTPError, OSError, ValueError):
                continue
            if not image_bytes:
                continue
            try:
                qrcode = await _decode_sgwcmaid_qrcode_image_isolated(image_bytes)
            except (OSError, ValueError):
                continue
            if qrcode:
                return qrcode
    return None


def _decode_stdin() -> int:
    """扫码子进程入口；二维码原文仅经 stdin/stdout 传递，不进入命令行。"""
    try:
        qrcode = decode_sgwcmaid_qrcode_image(sys.stdin.buffer.read())
    except Exception:
        return 1
    if qrcode:
        sys.stdout.write(qrcode)
    return 0


if __name__ == '__main__':
    raise SystemExit(_decode_stdin() if '--decode-stdin' in sys.argv else 2)
