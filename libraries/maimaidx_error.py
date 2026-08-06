from textwrap import dedent
from typing import Optional


MISSING_PLAYER_MESSAGE = dedent('''
    用户不存在：AWMC NET.、水鱼和落雪均未找到可用成绩。

    您可以打开微信中的「舞萌DX | 中二节奏」玩家二维码，
    长按二维码并选择「识别图中二维码」，复制识别出的字符或网页地址发送给 Bot。
    支持 SGWCMAID、wq.wahlap.net 的 img/req 链接。

    发送后会自动创建 AWMCNET 资料，无需额外操作。
''').strip()


class UserNotFoundError(Exception):

    def __str__(self) -> str:
        return dedent('''
            未找到此玩家，请确保此玩家的用户名和查分器中的用户名相同。
            如未绑定，请前往查分器官网进行绑定
            https://www.diving-fish.com/maimaidx/prober/
        ''').strip()


class LxnsDataError(UserNotFoundError):
    """
    落雪数据获取失败（未绑定 / 无授权 / 无数据）。
    继承 UserNotFoundError 以便被现有 except 捕获并返回自定义消息。
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class UserNotExistsError(Exception):

    def __str__(self) -> str:
        return MISSING_PLAYER_MESSAGE


class UserDisabledQueryError(Exception):

    def __str__(self) -> str:
        return '该用户禁止了其他人获取数据或未同意用户协议。'


class TokenError(Exception):

    def __str__(self) -> str:
        return '开发者Token有误'


class TokenDisableError(Exception):

    def __str__(self) -> str:
        return '开发者Token被禁用'


class QBindRequiredError(Exception):
    """官方 QQ 未绑定水鱼查分 QQ。"""

    def __init__(self, platform_id: str):
        self.platform_id = platform_id
        super().__init__(platform_id)

    def __str__(self) -> str:
        return (
            '你尚未绑定查分 QQ。\n'
            '请发送：qbind（论坛 OAuth，推荐）\n'
            '论坛邮箱请使用 你的QQ号@qq.com，授权后自动关联查分 QQ。\n'
            '可选：qbind 你的QQ号（仍须完成 OAuth，邮箱 QQ 必须一致）'
        )


class BreakInsufficientError(Exception):
    """BREAK 余额不足。"""

    def __init__(self, required: int, current: int, qqid: Optional[int] = None):
        self.required = required
        self.current = current
        self.qqid = qqid

    def __str__(self) -> str:
        from .maimaidx_break import format_break_insufficient_message
        return format_break_insufficient_message(self.qqid, self.required, self.current)


class TokenNotFoundError(Exception):

    def __str__(self) -> str:
        return '请先联系水鱼申请开发者token'


class MusicNotPlayError(Exception):
    
    def __str__(self) -> str:
        return '您未游玩该曲目'


class ServerError(Exception):

    def __str__(self) -> str:
        return '别名服务器错误，请联系插件开发者'


class EnterError(Exception):

    def __str__(self) -> str:
        return '参数输入错误'


class AliasesNotFoundError(Exception):
    
    def __str__(self) -> str:
        return '未找到别名'


class UnknownError(Exception):
    """未知错误"""


def format_command_error(e: Exception) -> str:
    """将异常转为可发给玩家的中文文案；已知业务异常（如 BREAK 不足）直接透传。"""
    if isinstance(e, (
        BreakInsufficientError,
        QBindRequiredError,
        UserNotFoundError,
        LxnsDataError,
        UserNotExistsError,
        UserDisabledQueryError,
        MusicNotPlayError,
        EnterError,
        AliasesNotFoundError,
        TokenError,
        TokenDisableError,
        TokenNotFoundError,
        ServerError,
    )):
        return str(e)
    import httpx
    if isinstance(e, (httpx.RemoteProtocolError, httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
        return '数据获取失败，无法反应本次查询成绩'
    return f'未知错误：{type(e).__name__}\n请联系Bot管理员'
