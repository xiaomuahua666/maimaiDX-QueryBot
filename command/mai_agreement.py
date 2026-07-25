"""AWMC 用户协议：链接、动态确认词、版本化同意与撤回；数据共享 opt-out。"""

from __future__ import annotations

from nonebot import on_command, on_message
from nonebot.adapters import Event
from nonebot.params import CommandArg
from nonebot.rule import Rule

from ..libraries.maimaidx_admin_audit import admin_audit
from ..libraries.maimaidx_data_share import data_share
from ..libraries.maimaidx_platform import billing_user_id


DEFAULT_AGREEMENT_URL = "https://wiki.awmc.team/guide/bot/terms"
DEFAULT_AGREEMENT_ACCEPT_TEXT = (
    "我已认真阅读网页中的服务说明，并已了解AWMC服务可能带来的风险。"
    "我了解因使用本服务，造成舞萌DX官方账号遭到封禁，责任和AWMC无关。"
    "我确认发送二维码可能会对我的账号产生安全影响，并愿意接受这样的风险。"
    "在阅读说明后，我同意上述协议。"
)
DEFAULT_AGREEMENT_VERSION = "4"
_LEGACY_DEFAULT_URL = "https://wiki.awmc.cc/guide/bot/terms"
_LEGACY_DEFAULT_VERSION = "2.0.0"

_DATA_SHARE_NOTE = (
    "\n\n📊 数据共享（默认开启）：\n"
    "玩家成绩、Rating、推分趋势等将脱敏后作为公开数据处理，"
    "用于同段统计（ARPI）、锐评样本优化与模型/提示词训练。\n"
    "若不想共享，请发送「不同意共享我的数据」；"
    "可随时发送「同意共享我的数据」重新开启。\n"
    "查询「数据共享状态」可查看当前开关。"
)


def agreement_policy() -> dict[str, str]:
    url = admin_audit.get_setting("agreement_url", DEFAULT_AGREEMENT_URL)
    version = admin_audit.get_setting("agreement_version", DEFAULT_AGREEMENT_VERSION)
    # 仅迁移旧版默认值，管理员自行设置的其它链接和版本保持不变。
    if url == _LEGACY_DEFAULT_URL:
        url = DEFAULT_AGREEMENT_URL
        admin_audit.set_setting("agreement_url", url)
    if version == _LEGACY_DEFAULT_VERSION:
        version = DEFAULT_AGREEMENT_VERSION
        admin_audit.set_setting("agreement_version", version)
    return {
        "url": url,
        "accept_text": admin_audit.get_setting(
            "agreement_accept_text", DEFAULT_AGREEMENT_ACCEPT_TEXT
        ),
        "version": version,
    }


def has_user_agreed(event: Event) -> bool:
    policy = agreement_policy()
    return admin_audit.has_agreed(str(billing_user_id(event)), policy["version"])


def agreement_prompt() -> str:
    policy = agreement_policy()
    return (
        f"📋 使用前请阅读 AWMC maiBot 服务协议（v{policy['version']}）：\n"
        f"{policy['url']}\n\n"
        "阅读网页后，请完整复制并发送网页中的确认词。\n"
        "请认真阅读本网页内容和协议。"
        + _DATA_SHARE_NOTE
    )


def _is_accept_phrase(event: Event) -> bool:
    try:
        text = event.get_plaintext().strip()
    except Exception:
        return False
    expected = agreement_policy()["accept_text"].strip()
    return bool(expected) and text == expected


agreement_view = on_command("用户协议", aliases={"mai用户协议"})
agreement_accept = on_command("同意用户协议")
agreement_phrase = on_message(rule=Rule(_is_accept_phrase), priority=3, block=True)
agreement_revoke = on_command("撤回用户协议")
data_share_opt_out = on_command(
    "不同意共享我的数据",
    aliases={"拒绝共享数据", "关闭数据共享", "不同意共享数据"},
)
data_share_opt_in = on_command(
    "同意共享我的数据",
    aliases={"开启数据共享", "同意共享数据"},
)
data_share_status = on_command("数据共享状态", aliases={"共享数据状态", "我的数据共享"})

for _debt_exempt_matcher in (
    agreement_view,
    agreement_accept,
    agreement_phrase,
    agreement_revoke,
    data_share_opt_out,
    data_share_opt_in,
    data_share_status,
):
    setattr(_debt_exempt_matcher, "_maimaidx_debt_exempt", True)


async def _accept(event: Event) -> str:
    uid = str(billing_user_id(event))
    version = agreement_policy()["version"]
    admin_audit.accept_agreement(uid, version)
    admin_audit.add_step("agreement.accept", "success", {"version": version})
    return (
        f"已确认并同意用户协议 v{version}。"
        + _DATA_SHARE_NOTE
    )


@agreement_view.handle()
async def _():
    await agreement_view.finish(agreement_prompt())


@agreement_accept.handle()
async def _(event: Event, args=CommandArg()):
    supplied = args.extract_plain_text().strip()
    if supplied != agreement_policy()["accept_text"].strip():
        await agreement_accept.finish(
            "确认词不正确。请发送“用户协议”打开链接，并完整复制网页中的确认词。"
        )
    await agreement_accept.finish(await _accept(event))


@agreement_phrase.handle()
async def _(event: Event):
    await agreement_phrase.finish(await _accept(event))


@agreement_revoke.handle()
async def _(event: Event):
    uid = str(billing_user_id(event))
    changed = admin_audit.revoke_agreement(uid)
    admin_audit.add_step("agreement.revoke", "success", {"changed": changed})
    await agreement_revoke.finish(
        "已撤回同意。已存储的绑定数据请另行使用 mai解绑处理。"
        if changed else "当前没有生效中的协议同意记录。"
    )


@data_share_opt_out.handle()
async def _(event: Event):
    uid = str(billing_user_id(event))
    changed = data_share.opt_out(uid)
    admin_audit.add_step("data_share.opt_out", "success", {"changed": changed})
    if changed:
        await data_share_opt_out.finish(
            "已关闭数据共享。\n"
            "你的成绩、Rating、推分趋势将不再进入脱敏公开数据集与同段样本聚合；"
            "个人查分、存档、周报等功能不受影响。\n"
            "如需重新开启，请发送「同意共享我的数据」。"
        )
    await data_share_opt_out.finish("你已关闭数据共享，无需重复操作。")


@data_share_opt_in.handle()
async def _(event: Event):
    uid = str(billing_user_id(event))
    changed = data_share.opt_in(uid)
    admin_audit.add_step("data_share.opt_in", "success", {"changed": changed})
    if changed:
        await data_share_opt_in.finish(
            "已重新开启数据共享。\n"
            "后续脱敏后的成绩数据可再次用于同段统计、锐评优化与公开数据集。"
        )
    await data_share_opt_in.finish("数据共享已是开启状态（默认）。")


@data_share_status.handle()
async def _(event: Event):
    uid = str(billing_user_id(event))
    admin_audit.add_step("data_share.status", "success", {})
    await data_share_status.finish(data_share.status_text(uid))
