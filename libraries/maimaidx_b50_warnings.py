"""
B50 生成结果诊断：无数据 / 水鱼掩码成绩等，在图片下方追加提示。

在 DrawBest 绘图前调用 prepare_b50_warnings()；footer 构建时 pop_b50_warning_footer() 取出。
"""

from __future__ import annotations

import contextvars
from typing import List, Optional

from .maimaidx_model import ChartInfo, UserInfo

_WARNINGS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    'b50_warnings', default=None
)

WARN_NO_DATA_FISH = (
    '⚠️ 疑似没有找到您的数据! 您可以这样做！\n'
    '确保在水鱼个人资料中填写了正确的QQ号，并关闭"禁止其他人查询我的成绩"。 '
    '绑定成功后如果没有数据！那么请执行一次 maiu.'
)

WARN_MASKED_FISH = (
    '⚠️ 您疑似开启了掩码\n'
    '确保在水鱼个人资料中关闭了"对非网页查询的成绩使用掩码"。然后重试。'
)

WARN_NO_DATA_LXNS = (
    '⚠️ 疑似没有找到您的数据! 请在落雪上传一次数据再来使用吧！\n'
    '您可以使用 maiul 来上传游戏数据！'
)

WARN_INCOMPLETE_AWMCNET = (
    '⚠️ 您的成绩不完整\n'
    '您可以直接发送SGWCMAID或二维码图片来更新您的准确成绩。'
)


def resolve_b50_source(qqid: Optional[int], username: Optional[str] = None) -> str:
    """返回 'divingfish'、'lxns' 或 'awmcnet'。"""
    if username:
        return 'divingfish'
    if qqid:
        try:
            from .maimaidx_datasource import get_user_source
            return get_user_source(qqid)
        except Exception:
            pass
    return 'divingfish'


def _chart_achievements(userinfo: UserInfo) -> List[float]:
    charts = userinfo.charts
    if not charts:
        return []
    records: List[ChartInfo] = list(charts.sd or []) + list(charts.dx or [])
    return [float(r.achievements) for r in records]


def is_empty_b50(userinfo: UserInfo) -> bool:
    return len(_chart_achievements(userinfo)) == 0


def is_masked_b50(userinfo: UserInfo) -> bool:
    """水鱼掩码成绩放大 10000 后末位是 0（如 100.4950→1004950 末位 0、100.0000→1000000 末位 0、99.5000→995000 末位 0）。"""
    achs = _chart_achievements(userinfo)
    if not achs:
        return False

    def _last_digit_is_zero(x: float) -> bool:
        return int(round(x * 10000)) % 10 == 0

    return all(_last_digit_is_zero(a) for a in achs)


def compute_b50_warning_text(userinfo: UserInfo, source: str) -> str:
    """根据 UserInfo 与数据源计算警告文案（不含 leading 空行）。"""
    if source == 'lxns':
        if is_empty_b50(userinfo):
            return WARN_NO_DATA_LXNS
        return ''

    if source == 'awmcnet':
        # AWMCNET 成绩来自镜像同步，空数据或被掩码的成绩都视为不完整，
        # 引导用户直接发送 SGWCMAID / 二维码更新准确成绩。
        if is_empty_b50(userinfo) or is_masked_b50(userinfo):
            return WARN_INCOMPLETE_AWMCNET
        return ''

    # 水鱼
    if is_empty_b50(userinfo):
        return WARN_NO_DATA_FISH
    if is_masked_b50(userinfo):
        return WARN_MASKED_FISH
    return ''


def prepare_b50_warnings(userinfo: UserInfo, source: str) -> None:
    """在 B50 绘图前调用，缓存本次生成的警告文案。"""
    text = compute_b50_warning_text(userinfo, source)
    _WARNINGS.set(text or None)


def clear_b50_warnings() -> None:
    _WARNINGS.set(None)


def pop_b50_warning_footer() -> str:
    """取出并清除警告文案（纯文本，不含 leading 空行）。"""
    text = _WARNINGS.get()
    _WARNINGS.set(None)
    return text or ''
