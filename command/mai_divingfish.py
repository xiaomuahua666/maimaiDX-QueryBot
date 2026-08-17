"""水鱼查分器 OAuth 授权。

「绑定水鱼」用于授权 Bot 查询用户本人的成绩；上传到水鱼仍使用
「mai绑定水鱼」/「maibindfish」保存 Import-Token。
"""

from textwrap import dedent

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

from ..config import log
from ..libraries.maimaidx_divingfish_oauth import (
    binding_label,
    create_device_authorization,
    oauth_enabled,
    revoke_url,
)
from ..libraries.maimaidx_error import (
    DivingFishOAuthError,
    QBindRequiredError,
)
from ..libraries.maimaidx_platform import resolve_score_qqid


def _oauth_prompt(url: str, qqid: int, expires_in: int) -> str:
    minutes = max(int(expires_in) // 60, 1)
    return dedent(
        f'''\
        请完成水鱼查分器授权：

        1. 打开以下链接并登录水鱼账号
        =======================
        {url}
        =======================
        2. 确认页面显示的绑定身份为「{binding_label(qqid)}」后点击「同意授权」

        链接 {minutes} 分钟内有效，授权完成后无需回复授权码。
        发送「刷新b50」可立即读取水鱼全量成绩并同步到 AWMCNET。
        请注意：这条链接仅供您本人使用，请勿转发他人。
        如需取消授权，请前往：{revoke_url()}
        '''
    ).strip()


df_bind = None

if oauth_enabled():
    df_bind = on_command(
        'dfbind',
        aliases={'绑定水鱼', '绑定df', '水鱼授权'},
        block=True,
    )

    @df_bind.handle()
    async def _handle_df_bind(
        event: MessageEvent,
        args: Message = CommandArg(),
    ):
        if args.extract_plain_text().strip():
            await df_bind.finish(
                '水鱼 OAuth 绑定不需要参数，请直接发送「绑定水鱼」。\n'
                '如需绑定 Import-Token，请发送「mai绑定水鱼 <Token>」。',
                reply_message=True,
            )
        try:
            qqid = resolve_score_qqid(event)
            authorization = await create_device_authorization(qqid)
        except QBindRequiredError as exc:
            await df_bind.finish(str(exc), reply_message=True)
        except (DivingFishOAuthError, RuntimeError, OSError) as exc:
            log.warning(f'[divingfish-oauth] authorization failed: {type(exc).__name__}')
            await df_bind.finish(
                '发起水鱼授权失败：水鱼账号服务可能暂时不可用，请稍后再试。',
                reply_message=True,
            )

        await df_bind.finish(
            _oauth_prompt(
                authorization.verification_uri_complete,
                qqid,
                authorization.expires_in,
            ),
            reply_message=True,
        )
