from __future__ import annotations

import re

from .domain import StyleSpec

_INJECTION_PATTERNS = (
    r"ignore\s+(all|any|the|previous|above)",
    r"忽略(之前|以上|上面|先前)(的)?(指令|规则|提示词)?",
    r"不要遵守(安全|系统|审核)?规则",
    r"(泄露|显示|打印|输出).{0,12}(系统提示词|system prompt|内部指令)",
    r"(开发者模式|越狱模式|jailbreak|developer mode|DAN)",
    r"把(违法|违规|色情|暴力|仇恨|自杀).{0,16}(包装|伪装|写成|改成)",
)
_UNSAFE_PATTERNS = (
    r"(制作|购买|交易).{0,12}(炸弹|枪|毒品|违禁品)",
    r"(自杀|自残|杀人).{0,12}(方法|教程|步骤)",
    r"(色情|裸聊|约炮|成人视频|性服务)",
    r"(种族|民族|性别|地域).{0,8}(歧视|仇恨)",
    r"(诈骗|洗钱|盗号|赌博网站).{0,12}(教程|方法|引流|推广)",
    r"(未成年|儿童).{0,8}(色情|性行为|裸照)",
    r"(死全家|去死吧|你妈死了|支那|黑鬼)",
)
_STYLE_HINTS = {
    "可爱": "可爱、轻快",
    "猫娘": "可爱、亲近，适度使用猫娘语气",
    "女仆": "礼貌、亲近，适度服务感",
    "傲娇": "轻微傲娇，不攻击用户",
    "毒舌": "直接、有吐槽感，但不做人身攻击",
    "温柔": "温柔、鼓励，仍然指出问题",
    "教练": "强调训练优先级和可执行建议",
    "数据": "少修辞，多展示证据和数字",
    "主人": "称呼用户为主人",
    "喵": "句尾偶尔使用喵，不连续堆叠",
}


def _matches(text: str, patterns: tuple[str, ...]) -> list[str]:
    return [p for p in patterns if re.search(p, text, flags=re.I)]


def scan_text(text: str) -> dict:
    raw = str(text or "").strip()
    injections = _matches(raw, _INJECTION_PATTERNS)
    unsafe = _matches(raw, _UNSAFE_PATTERNS)
    return {
        "allowed": not injections and not unsafe,
        "injection": bool(injections),
        "unsafe": bool(unsafe),
        "category": "prompt_injection" if injections else ("unsafe" if unsafe else None),
    }


def normalize_style(text: str, *, max_length: int = 240) -> StyleSpec:
    raw = " ".join(str(text or "").split())[:max_length]
    if not raw:
        return StyleSpec()
    verdict = scan_text(raw)
    if not verdict["allowed"]:
        raise ValueError("风格描述包含不可执行的控制指令或违规内容，请换一种描述方式喵。")

    hints = [value for key, value in _STYLE_HINTS.items() if key.casefold() in raw.casefold()]
    tone = "；".join(dict.fromkeys(hints)) or "自然、熟悉舞萌的朋友口吻"
    address = "主人" if "主人" in raw else ""
    suffix = "喵" if "喵" in raw or "猫娘" in raw else ""
    sharpness = 4 if any(x in raw for x in ("毒舌", "辛辣", "狠狠")) else 3
    warmth = 4 if any(x in raw for x in ("温柔", "鼓励", "关心")) else 3
    humor = 3 if any(x in raw for x in ("可爱", "猫娘", "幽默", "搞笑")) else 2
    focus = tuple(x for x in ("训练", "推分", "稳定性", "上限", "数据") if x in raw)
    return StyleSpec(
        raw=raw,
        direction=raw,
        tone=tone,
        sharpness=sharpness,
        warmth=warmth,
        humor=humor,
        address=address,
        suffix=suffix,
        focus=focus,
    )


def validate_report_text(text: str) -> dict:
    raw = str(text or "")
    verdict = scan_text(raw)
    return {"safe": verdict["allowed"], "category": verdict["category"]}
