"""图片二维码识别回归测试（需安装项目依赖 zxing-cpp）。"""

import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageEnhance, ImageFilter
import zxingcpp

from libraries.maimaidx_qrcode_util import (
    _safe_remote_image_url,
    decode_sgwcmaid_qrcode_image,
    extract_sgwcmaid_from_image_segments,
)


SGWCMAID = (
    "SGWCMAID26071514110240B055073BAFB054595882DF610F3D7CF2EECF402B9302ECF55C61972E55C7B8"
)


def qr_png(text: str, scale: int = 5) -> bytes:
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    image = Image.fromarray(barcode.to_image(scale=scale))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


raw_png = qr_png(SGWCMAID)
assert decode_sgwcmaid_qrcode_image(raw_png) == SGWCMAID

official_url = (
    "https://wq.wahlap.net/qrcode/img/"
    "MAID26071514110240B055073BAFB054595882DF610F3D7CF2EECF402B9302ECF55C61972E55C7B8.png?v"
)
assert decode_sgwcmaid_qrcode_image(qr_png(official_url)) == SGWCMAID
assert decode_sgwcmaid_qrcode_image(qr_png("https://example.com/not-maimai")) is None


def render_low_quality_png(text: str) -> bytes:
    """模拟手机拍屏 + 群图压缩：小尺寸、低对比、轻微模糊、JPEG 压缩。"""
    barcode = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
    img = Image.fromarray(barcode.to_image(scale=3)).convert("RGB")
    img = img.resize((280, 280), Image.Resampling.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.9))
    img = ImageEnhance.Contrast(img).enhance(0.55)
    img = ImageEnhance.Brightness(img).enhance(1.15)
    output = BytesIO()
    img.save(output, format="JPEG", quality=45)
    return output.getvalue()


low_quality_bytes = render_low_quality_png(SGWCMAID)
assert decode_sgwcmaid_qrcode_image(low_quality_bytes) == SGWCMAID, (
    "低质量图片二维码兜底解码失败"
)

segment = SimpleNamespace(
    type="image",
    data={"file": "base64://" + base64.b64encode(raw_png).decode()},
)
assert asyncio.run(extract_sgwcmaid_from_image_segments([segment])) == SGWCMAID
assert _safe_remote_image_url("https://gchat.qpic.cn/example.png")
assert not _safe_remote_image_url("http://127.0.0.1/secret.png")
assert not _safe_remote_image_url("http://localhost/secret.png")

print("image QR recognition tests: ok")
