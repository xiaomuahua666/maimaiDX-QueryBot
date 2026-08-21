from __future__ import annotations

import asyncio
import json
import re

from typing import Any

from loguru import logger as log
from openai import AsyncOpenAI, BadRequestError

from ..maimaidx_llm_runtime import resolve_llm_runtime_config

_FORBIDDEN_OUTPUT_PATTERNS = [
    "综上所述", "整体来看", "值得称赞", "值得一提", "由此可见", "不难看出",
    "毋庸置疑", "首先", "其次", "与其说", "不如说",
    "w5低", "w5中", "w5高", "w6低", "w6中", "w6高", "w5", "w6", "15k", "16k",
    "AP数量", "AP 数量", "AP总数", "AP 总数", "FC数量", "FC 数量", "FC总数", "FC 总数",
    "没有 AP", "没 AP", "0 AP", "AP 挂零", "没有AP", "没AP",
]

_SUNNY_STYLE_MARKERS = [
    "OneCat", "家人们", "你告诉我", "有没有可能", "就你看", "那我只能说", "某种程度上",
    "虚低", "割裂", "榜样", "开香槟", "通透", "伟大", "变态", "疯了", "固若金汤",
    "瞻仰", "重量级", "我人直接傻", "是真看不懂", "咱就说", "一点毛病没有", "保守", "吃透",
    "匹配不到一块", "营养美味", "众生百态", "直接给你封", "重点表扬",
]

_SUNNY_PRAISE_MARKERS = [
    "伟大", "变态", "疯了", "榜样", "开香槟", "固若金汤", "重量级", "瞻仰",
    "通透", "吃透", "行业标杆", "淋漓尽致", "我人直接傻", "是真看不懂",
]

_SUNNY_SPOKEN_MARKERS = [
    "你告诉我", "有没有可能", "那我只能说", "就你看", "咱就说", "嘶", "哎", "对吧", "是吧",
]

_SUNNY_SHOW_MARKERS = [
    "家人们", "瞻仰", "我人直接傻", "是真看不懂", "开香槟", "重量级",
    "往下一滑", "结果你这一看", "这就有味", "直接给你封", "重点表扬",
]

_REPORT_TONE_TERMS = [
    "说明", "结构", "匹配", "健康", "综合来看", "分析可见", "数据表明", "整体表现",
]

_PUSH_TAGS = {
    "theme": "你点的菜",
    "practice": "练手磨配置",
    "strong": "强项放大",
    "weak": "弱项补课",
    "overall": "综合推荐",
}

_STYLE_STOPWORDS = {
    "分析", "一下", "帮我", "看看", "给我", "我想", "想要", "适合", "谱面", "推分",
    "推荐", "需求", "问题", "风格", "语气", "长版", "短版", "版本", "口吻", "锐评",
}


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _response_token_usage(response: Any) -> dict[str, Any]:
    """兼容 OpenAI Chat Completions 及部分兼容网关的 usage 字段。"""
    def field(value: Any, *names: str) -> Any:
        for name in names:
            item = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
            if item is not None:
                return item
        return None

    usage = field(response, "usage")
    if usage is None:
        return {
            "available": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
        }
    input_tokens = _i(field(usage, "prompt_tokens", "input_tokens"))
    output_tokens = _i(field(usage, "completion_tokens", "output_tokens"))
    total_tokens = _i(field(usage, "total_tokens"))
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    prompt_details = field(
        usage, "prompt_tokens_details", "input_tokens_details"
    )
    cached_input_tokens = _i(field(prompt_details, "cached_tokens"))
    return {
        # 只有 total_tokens 无法按输入/输出差异定价，视为 usage 不完整并走兜底价。
        "available": input_tokens > 0 or output_tokens > 0,
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
        "cached_input_tokens": max(0, cached_input_tokens),
    }


def _song_key(song: dict) -> str:
    mid = str(song.get("music_id") or song.get("song_id") or song.get("musicId") or "").strip()
    level_index = _i(song.get("level_index"), -1)
    return f"{mid}:{level_index}" if mid else ""


def _song_tags(song: dict) -> list[str]:
    return [str(t).strip() for t in (song.get("config_tags") or song.get("keywords") or song.get("config") or []) if str(t).strip()]


def _ach_pct(song: dict) -> float:
    ach = _f(song.get("achievement", song.get("achievements")), 0.0)
    return ach / 10000.0 if ach > 200 else ach


def _clean_text(value: str, limit: int = 0) -> str:
    text = _sanitize_rating_terms(str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if limit > 0:
        return text[:limit].strip()
    return text


def _extract_user_focus_terms(user_message: str) -> set[str]:
    raw = re.split(r"[\s,，。！？!?:：/|、（）()\[\]【】<>《》'\"；;]+", str(user_message or ""))
    terms: set[str] = set()
    for part in raw:
        token = part.strip()
        if not token or token in _STYLE_STOPWORDS:
            continue
        if token in _FORBIDDEN_OUTPUT_PATTERNS:
            continue
        if len(token) <= 1 and not re.search(r"[A-Za-z0-9+]", token):
            continue
        terms.add(token)
    return terms


def _has_any_tag(song: dict, wanted: set[str]) -> bool:
    if not wanted:
        return False
    title = str(song.get("title") or "").lower()
    tags = [t.lower() for t in _song_tags(song)]
    for want in wanted:
        want_low = str(want).strip().lower()
        if not want_low:
            continue
        if want_low in title:
            return True
        for tag in tags:
            if want_low == tag or want_low in tag or tag in want_low:
                return True
    return False


def _normalize_strategy_tag(value: str) -> str:
    tag = _clean_text(value)
    if tag in _PUSH_TAGS.values():
        return tag
    for normalized in _PUSH_TAGS.values():
        if tag and (tag in normalized or normalized in tag):
            return normalized
    return _PUSH_TAGS["overall"]


def _default_push_reason(song: dict, strategy_tag: str) -> str:
    import random as _rand

    tags = "/".join(_song_tags(song)[:3]) or "配置"
    ach = _ach_pct(song)
    ds = _f(song.get("ds"), 0.0)
    target = str(song.get("target") or "").upper()
    target_word = "补鸟加" if target == "SSS+" else ("补鸟" if target == "SSS" else "吃分")
    gain = _i(song.get("estimated_gain"), 0) or (
        _i(song.get("gain_1005"), 0)
        if target == "SSS+"
        else _i(song.get("gain_100"), 0)
    )
    ach_gap = _f(song.get("ach_gap"), 0.0)
    close_hint = "已经寸止" if ach_gap and ach_gap <= 0.2 else "差一点就能推"

    if strategy_tag == _PUSH_TAGS["theme"]:
        reasons = [
            f"{tags}正好对口，{ds:.1f}定数{ach:.1f}%，这波{target_word}血赚。",
            f"你点名的{tags}来了，定数{ds:.1f}才{ach:.1f}%，寸止位直接{target_word}。",
            f"{tags}谱定数{ds:.1f}，{ach:.1f}%差一口气，趁热{target_word}。",
        ]
    elif strategy_tag == _PUSH_TAGS["practice"]:
        reasons = [
            f"{tags}练手谱，{ds:.1f}不超纲先拿来补补。",
            f"{ds:.1f}定数{tags}顺手，拿来磨配置正好。",
            f"{tags}下位谱，{ds:.1f}顺手不费力，先稳一手。",
        ]
    elif strategy_tag == _PUSH_TAGS["strong"]:
        reasons = [
            f"{tags}本来就是你的强项，{close_hint}，{target_word}直接收。",
            f"你{tags}已经很猛了，{ds:.1f}这张{close_hint}，顺手{target_word}。",
            f"强项{tags}再来一张，{ds:.1f}定数{ach:.1f}%，{target_word}放大优势。",
        ]
    elif strategy_tag == _PUSH_TAGS["weak"]:
        reasons = [
            f"{tags}是你短板，{ds:.1f}这张下位谱正好拿来修地板。",
            f"弱项{tags}得练，{ds:.1f}定数先把这张{target_word}。",
            f"{tags}不太行？{ds:.1f}这张难度刚好，拿来补短板。",
        ]
    else:
        reasons = [
            f"{ds:.1f}定数正好卡你能力段，{close_hint}{target_word}进 B50，收益约{gain}。",
            f"定数{ds:.1f} {tags}，{ach:.1f}%差一点，{target_word}就能往上推。",
            f"{ds:.1f}这张{close_hint}，收益{gain}，性价比很高。",
        ]

    return _rand.choice(reasons)


def _prepare_push_song(song: dict, strategy_tag: str, reason: str | None = None) -> dict:
    merged = dict(song)
    merged["strategy_tag"] = _normalize_strategy_tag(strategy_tag)
    final_reason = _clean_text(reason or merged.get("reason") or merged.get("recommend_reason"), 20)
    if not final_reason:
        final_reason = _default_push_reason(merged, merged["strategy_tag"])
    merged["reason"] = final_reason
    merged["recommend_reason"] = final_reason
    merged["achievement"] = round(_ach_pct(merged), 4)
    merged["achievements"] = merged["achievement"]
    merged["music_id"] = str(merged.get("music_id") or merged.get("song_id") or merged.get("musicId") or "")
    return merged


def _select_push_recommendations(candidates: list[dict], config_profile: dict, user_message: str, limit: int = 3) -> list[dict]:
    limit = min(3, max(0, int(limit)))
    filtered = [dict(s) for s in (candidates or []) if isinstance(s, dict) and _ach_pct(s) < 100.5]
    if not filtered or limit == 0:
        return []

    focus_terms = _extract_user_focus_terms(user_message)
    strong_tags = {
        str(item.get("config") or item.get("tag") or "").strip()
        for item in (config_profile.get("strong") or [])
        if str(item.get("config") or item.get("tag") or "").strip()
    }
    weak_tags = {
        str(item.get("config") or item.get("tag") or "").strip()
        for item in (config_profile.get("weak") or [])
        if str(item.get("config") or item.get("tag") or "").strip()
    }

    def _overall_score(song: dict) -> tuple:
        # 与 sorted(..., reverse=True) 配合：数值越大越优先
        return (
            -_f(song.get("ds_fit"), 99.0),
            -_f(song.get("ach_gap"), 99.0),
            _i(song.get("estimated_gain"), 0),
            len(_song_tags(song)),
            -_i(song.get("play_count", song.get("playCount")), 0),
        )

    theme_pool = [s for s in filtered if _has_any_tag(s, focus_terms)]
    strong_pool = [s for s in filtered if _has_any_tag(s, strong_tags)]
    weak_pool = [s for s in filtered if _has_any_tag(s, weak_tags)]
    practice_pool = sorted(weak_pool or filtered, key=_overall_score, reverse=True)
    regular_pool = sorted(filtered, key=_overall_score, reverse=True)
    theme_pool.sort(key=_overall_score, reverse=True)
    strong_pool.sort(key=_overall_score, reverse=True)
    weak_pool.sort(key=_overall_score, reverse=True)

    result: list[dict] = []
    seen: set[str] = set()

    def _add_from(pool: list[dict], tag: str) -> bool:
        for song in pool:
            key = _song_key(song)
            if not key or key in seen:
                continue
            result.append(_prepare_push_song(song, tag))
            seen.add(key)
            return True
        return False

    if focus_terms and theme_pool:
        for song in theme_pool[:2]:
            key = _song_key(song)
            if not key or key in seen:
                continue
            result.append(_prepare_push_song(song, _PUSH_TAGS["theme"]))
            seen.add(key)
            if len(result) >= min(limit, 2):
                break

    if len(result) < limit:
        _add_from(practice_pool, _PUSH_TAGS["practice"])
    if len(result) < limit:
        _add_from(strong_pool, _PUSH_TAGS["strong"])
    if len(result) < limit:
        _add_from(weak_pool, _PUSH_TAGS["weak"])

    for song in regular_pool:
        if len(result) >= limit:
            break
        key = _song_key(song)
        if not key or key in seen:
            continue
        result.append(_prepare_push_song(song, _PUSH_TAGS["overall"]))
        seen.add(key)

    return result[:limit]


def _merge_push_recommendations(raw_items: list, fallback_items: list[dict]) -> list[dict]:
    # Closed-world merge: the model may choose a candidate and rewrite its
    # reason, but it may never introduce a song or overwrite factual fields.
    # The 100.5 boundary is repeated here as defence in depth in case a caller
    # supplies an unfiltered fallback list.
    fallback_list = [
        dict(item)
        for item in (fallback_items or [])
        if isinstance(item, dict) and _ach_pct(item) < 100.5
    ]
    by_key = {_song_key(item): dict(item) for item in fallback_list if _song_key(item)}
    by_title = {_clean_text(item.get("title"), 80).lower(): dict(item) for item in fallback_list if _clean_text(item.get("title"), 80)}

    merged: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        raw_id = str(raw.get("music_id") or raw.get("song_id") or raw.get("musicId") or "").strip()
        raw_level_index = _i(raw.get("level_index"), -1)
        raw_title = _clean_text(raw.get("title"), 80)
        lookup_key = f"{raw_id}:{raw_level_index}" if raw_id else ""
        base = dict(by_key.get(lookup_key) or by_title.get(raw_title.lower()) or {})
        # No fuzzy title matching and no model-provided card fallback: both
        # paths previously allowed an invented or similarly named song in.
        if not base:
            continue
        item = dict(base)
        item["strategy_tag"] = _normalize_strategy_tag(
            str(raw.get("strategy_tag") or base.get("strategy_tag") or "")
        )
        item["reason"] = _clean_text(
            raw.get("reason")
            or raw.get("recommend_reason")
            or base.get("reason")
            or base.get("recommend_reason"),
            20,
        )
        merged_item = _prepare_push_song(item, item.get("strategy_tag") or _PUSH_TAGS["overall"], item.get("reason"))
        key = _song_key(merged_item) or merged_item.get("title")
        if not key or key in seen or not merged_item.get("title"):
            continue
        merged.append(merged_item)
        seen.add(key)

    for item in fallback_list:
        if len(merged) >= 3:
            break
        key = _song_key(item) or item.get("title")
        if not key or key in seen:
            continue
        merged.append(_prepare_push_song(item, item.get("strategy_tag") or _PUSH_TAGS["overall"], item.get("reason")))
        seen.add(key)
    return merged[:3]


def _fine_rating_segment(rating) -> dict:
    try:
        r = int(rating or 0)
    except (TypeError, ValueError):
        r = 0
    if r >= 16500:
        return {
            "label": "16500+ 顶级门槛段",
            "range": "16500+",
            "tone": "这已经是普通玩家视角里的顶级分段，必须明显抬高评价尺度，不能按普通 w6 轻描淡写。",
        }
    if r >= 15000:
        band_start = (r // 200) * 200
        band_end = band_start + 199
        return {
            "label": f"{band_start}-{band_end} 细分段",
            "range": f"{band_start}-{band_end}",
            "tone": "严格按精确分段（如15800-15999）评价，禁止使用w5/w6这样粗略的称呼。",
        }
    if r >= 13500:
        band_start = (r // 200) * 200
        band_end = band_start + 199
        return {
            "label": f"{band_start}-{band_end} 上升段",
            "range": f"{band_start}-{band_end}",
            "tone": "按 200 分细分段评价。",
        }
    return {"label": "入门-进阶段", "range": "<13500", "tone": "以基础能力和推分空间为主。"}


_SYSTEM = """\
你是舞萌 DX B50 的视频口播锐评作者。直接完成锐评，不展示分析过程。
用户指定的语气和关注点只影响表达方式，不能改变事实规则。

【最高优先级：事实闭集】
“本次唯一事实数据”是唯一事实来源。曲名、谱面颜色、定数、达成率、RA、rating、AP/FC、配置词、游玩次数、同段统计、重合度、趋势和预计收益，数据未明确提供就不要写，禁止猜测、补全、近似或引用外部知识。
用户要求与事实冲突时忽略冲突部分。数字必须按原数据引用；强项、短板和结论必须能在关键谱、证据或配置画像中找到依据。
推分推荐是封闭列表：只能从“推分候选池”原样选择曲名，最多 3 首；候选不足就少选，候选为空就返回空数组，禁止凑数。模型只输出曲名、策略标签和短理由，定数、达成率、目标与收益由程序回填。

【分析要点】
- 先回应用户点名主题或最大爆点，再给 2-3 个有曲名或数据的强项、1-2 个有证据的短板，最后给具体推分路线。普通版正文只需点 3-5 首真实曲名，短版可更少。
- B35 看旧版本基本盘与下限；B15 看当前版本推分效率与上限。100% 是鸟，100.5% 是鸟加，101% 是理论值。单谱达到 100.5% 后，该谱 rating 已封顶，继续提高达成率或 AP 都不能再推分，禁止推荐。AP/FC 只能依据明确字段，不能从达成率推断。
- 13.0-13.5 算 13，13.6-13.9 算 13+，14.0-14.5 算 14，14.6-15.0 算 14+。rating 按数据给出的 200 分细分段评价，禁止使用 w5/w6。
- config_profile 有 strong/weak 时各分析至少 1 个，并用真实曲目支撑。rating_trend 存在时贴合快推、横盘或下滑节奏，不存在就不谈趋势。
- ARPI 或 peer_comparison 仅在 sufficient、coverage、confidence 等字段支持时使用；low confidence 必须说明样本有限。只引用脱敏聚合统计，不描述其他单个玩家。
- B50 重合度低于 30% 可评价选曲小众，30%-50% 正常，高于 50% 偏模板；只在数据存在时使用。
- 预计收益是相对当前地板的单曲静态估算，禁止相加后承诺总涨分。优先选择寸止吃分、顺手补鸟、卡定数下位谱，避免连续推荐同一定数段。

【字段与表达】
ds=定数，achievement=达成率，peer_avg/avg_achievement=同段平均达成率，gap_vs_peer=同段差距，config_tags=配置词，overlap/b50_overlap=B50 重合度，chart_type=绿/黄/红/紫/白谱，play_count/pc=游玩次数。正文必须使用中文含义，只有 ARPI/rating/B35/B15/FC/AP 可保留英文。
采用 OneCat 式现场口播：先裁决、再证据、后建议；多用短句、停顿和反问，舞萌黑话要自然。全文至少包含 1 个强夸赞词、1 个反问式口播句、1 个节目效果转场，但不要堆口号。用户指定口吻时贯穿全文。
避免报告腔和“首先/其次/综上所述/整体来看”等套话；不要自我介绍、来源、步骤、免责声明、泛比喻或虚构机厅场景。不得输出违法、淫秽、隐私或擦边内容。

【严格输出预算】
- 只输出一个可被 json.loads() 解析的 JSON 对象，禁止 Markdown、代码块、前后解释或额外字段。
- JSON 仅包含 title、overall_roast、impression_roast、push_recommendations。
- title 为 10-18 个汉字，带舞萌语境；impression_roast 不超过 25 个汉字。
- overall_roast 为单行中文口播。普通版严格控制在 450-650 个汉字；用户明确要求短版时严格控制在 250-350 个汉字。至少使用 6 对成对且不嵌套的 <r>关键词</r>。
- push_recommendations 最多 3 首，每项仅包含 title、strategy_tag、reason。strategy_tag 只能是你点的菜/练手磨配置/强项放大/弱项补课/综合推荐；reason 控制在 12-20 个汉字，不写任何数值字段。
- 如果内容接近预算上限，优先删除修辞、转场、社区梗和次要证据，不得继续扩写；事实正确、JSON 完整和长度限制优先于文风。
{style_instruction}"""


def _sanitize_rating_terms(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?<![A-Za-z0-9])16\s*[kK](?![A-Za-z0-9])", "w6", value)
    value = re.sub(r"(?<![A-Za-z0-9])15\s*[kK](?![A-Za-z0-9])", "w5", value)
    value = re.sub(r"(?<![-\d])16[0-4]\d{2}(?![\d-])", "w6", value)
    value = re.sub(r"(?<![-\d])15\d{3}(?![\d-])", "w5", value)
    value = re.sub(r"(?<!\d)1[7-9]\d{3}(?!\d)", "顶段", value)
    value = value.replace("```json", "").replace("```", "")
    return value


def _cleanup_response(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text, flags=re.I)
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return _sanitize_rating_terms(text)
        try:
            data = json.loads(m.group(0))
        except Exception:
            return _sanitize_rating_terms(text)

    push_rows = data.get("push_recommendations")
    if not isinstance(push_rows, list):
        push_rows = []

    cleaned = {
        "title": _sanitize_rating_terms(str(data.get("title") or "")).replace("\r", " ").replace("\n", " ").strip(),
        "overall_roast": _sanitize_rating_terms(str(data.get("overall_roast") or "")).replace("\r", " ").replace("\n", " ").strip(),
        "impression_roast": _sanitize_rating_terms(str(data.get("impression_roast") or "")).replace("\r", " ").replace("\n", " ").strip(),
        "push_recommendations": [
            {
                "title": _clean_text(str(item.get("title") or ""), 80),
                "strategy_tag": _normalize_strategy_tag(str(item.get("strategy_tag") or "")),
                "reason": _clean_text(str(item.get("reason") or item.get("recommend_reason") or ""), 20),
                **({"music_id": str(item.get("music_id") or item.get("song_id") or item.get("musicId") or "")} if str(item.get("music_id") or item.get("song_id") or item.get("musicId") or "") else {}),
                **({"level_index": _i(item.get("level_index"), -1)} if item.get("level_index") is not None else {}),
                **({"ds": round(_f(item.get("ds"), 0.0), 1)} if item.get("ds") is not None else {}),
                **({"achievement": round(_ach_pct(item), 4)} if item.get("achievement") is not None or item.get("achievements") is not None else {}),
                **({"target": _clean_text(str(item.get("target") or ""), 12)} if str(item.get("target") or "").strip() else {}),
                **({"gain_100": _i(item.get("gain_100"), 0)} if item.get("gain_100") is not None else {}),
                **({"gain_1005": _i(item.get("gain_1005"), 0)} if item.get("gain_1005") is not None else {}),
            }
            for item in push_rows if isinstance(item, dict)
        ],
    }
    return json.dumps(cleaned, ensure_ascii=False)


def _validated_analysis_payload(raw_content: str) -> dict[str, Any]:
    content = str(raw_content or "").strip()
    if not content:
        raise ValueError("模型未返回锐评正文，请稍后重试")

    cleaned_content = _cleanup_response(content)
    try:
        cleaned = json.loads(cleaned_content)
    except json.JSONDecodeError:
        cleaned = {
            "title": "B50锐评",
            "overall_roast": cleaned_content,
            "impression_roast": "",
            "push_recommendations": [],
        }

    if not isinstance(cleaned, dict):
        raise ValueError("模型返回的锐评格式无效，请稍后重试")
    if not str(cleaned.get("overall_roast") or "").strip():
        raise ValueError("模型返回的锐评正文为空，请稍后重试")
    return cleaned


def _reasoning_effort(config: Any) -> str:
    value = str(getattr(config, "b50_llm_reasoning_effort", "low") or "").strip().lower()
    return value if value in {"none", "minimal", "low", "medium", "high"} else "low"


def _finish_reason(response: Any) -> str:
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not choices:
        return ""
    choice = choices[0]
    value = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
    return str(value or "").strip().lower()


def _message_field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def _fmt(context: dict) -> str:
    player = context.get("player") or {}
    summary = context.get("summary") or {}
    peer = context.get("peer_stats") or {}
    pack = context.get("b50_evidence_pack") or {}

    rating_val = player.get("rating")
    fine_seg = _fine_rating_segment(rating_val)

    lines = [
        f"玩家：{player.get('nickname')}  Rating：{rating_val}",
        f"分段判断：{fine_seg.get('label')}  {fine_seg.get('tone')}",
        f"B35 RA：{summary.get('b35_ra')}  B15 RA：{summary.get('b15_ra')}",
        f"全B50平均达成：{summary.get('avg_achievement')}%  平均定数：{summary.get('avg_ds')}",
        f"B35均值：{(summary.get('b35') or {}).get('avg_achievement')}%  B15均值：{(summary.get('b15') or {}).get('avg_achievement')}%",
    ]

    arpi = peer.get("arpi")
    overlap = (peer.get("b50_overlap") or {}).get("value")
    if arpi is not None:
        lines.append(f"ARPI：{arpi:+.4f}  B50重合度：{overlap:.2f}%")

    # ARPI bucket stats
    arpi_bucket = context.get("arpi_bucket_stats") or {}
    if arpi_bucket.get("sufficient"):
        pos = arpi_bucket.get("position", "")
        pos_label = {"above_p75": "同段上四分位/稳手", "around_median": "典型画风", "below_p25": "下四分位/靠选谱拉分"}.get(pos, pos)
        lines.append(f"ARPI同段位置：{pos_label}  均值：{arpi_bucket.get('mean')}  中位：{arpi_bucket.get('median')}")
    elif arpi_bucket:
        lines.append("ARPI同段：样本不足，先不硬下判断")

    # 真实推分趋势（来自本地存档，用于可行性判断）
    trend = context.get("rating_trend") or {}
    points = trend.get("points") or []
    if points:
        first = points[0]
        last = points[-1]
        delta = trend.get("delta")
        delta_txt = f"{delta:+d}" if isinstance(delta, int) else "未知"
        lines.append(
            f"推分趋势：{first.get('date')}({first.get('rating')}) → "
            f"{last.get('date')}({last.get('rating')})  窗口Δ={delta_txt}  "
            f"样本点={trend.get('point_count')}"
        )
        if trend.get("feasibility_hint"):
            lines.append(f"可行性提示：{trend.get('feasibility_hint')}")
            lines.append("推分建议必须参考上述趋势，不要脱离真实涨分节奏空喊猛推。")

    # B50 overlap interpretation
    b50_overlap = context.get("b50_overlap") or {}
    if isinstance(b50_overlap, dict) and b50_overlap.get("value") is not None:
        ov = float(b50_overlap.get("value") or 0)
        if ov < 30:
            ov_desc = "选曲小众/口味独到/谱面含金量高（正面）"
        elif ov <= 50:
            ov_desc = "正常区间"
        else:
            ov_desc = "偏模板/跟风攻略"
        lines.append(f"B50重合度：{ov:.2f}%  解读：{ov_desc}")

    peer_comp = pack.get("peer_comparison") or {}
    if peer_comp.get("matched") is not None:
        lines.append(
            f"同段样本：玩家 {peer_comp.get('player_count')} 人  "
            f"匹配谱面 {peer_comp.get('matched')}/{peer_comp.get('b50_chart_count')}  "
            f"覆盖率 {_f(peer_comp.get('coverage')) * 100:.1f}%  "
            f"证据置信度 {peer_comp.get('confidence')}（{peer_comp.get('confidence_text')}）"
        )
        if peer_comp.get("generated_at"):
            lines.append(f"同段统计生成时间：{peer_comp.get('generated_at')}")
    if peer_comp.get("available") is False:
        lines.append("同段统计：不可用时不要硬写 ARPI/gap")

    rating_split = pack.get("rating_split") or {}
    fine_segment = rating_split.get("fine_segment") or {}
    if fine_segment:
        lines.append(f"分段判断（pack）：{fine_segment.get('label')}  {fine_segment.get('tone')}")

    def _fmt_tags(tags: list) -> str:
        items = [str(t).strip() for t in (tags or []) if str(t).strip()]
        return "/".join(items[:4])

    def _chart_line(c: dict) -> str:
        gap = c.get("gap_vs_peer")
        peer_avg = c.get("peer_avg")
        tags = _fmt_tags(c.get("config_tags") or c.get("config") or [])
        parts = [f"[{c.get('bucket', '')} {c.get('ds', '')}] {c.get('title', '')}"]
        parts.append(f"{c.get('achievement', 0):.4f}%")
        parts.append(f"RA {c.get('song_rating', 0)}")
        if peer_avg is not None:
            parts.append(f"同段均值 {peer_avg:.4f}%")
        if gap is not None:
            parts.append(f"同段差距 {gap:+.4f}")
        if c.get("peer_sample_count") is not None:
            parts.append(f"该谱同段样本 {c.get('peer_sample_count')} 人")
        if tags:
            parts.append(f"配置 {tags}")
        return "  ".join(parts)

    # Config profile (strong/weak)
    config_profile = context.get("config_profile") or {}
    if config_profile.get("strong") or config_profile.get("weak"):
        lines.append("")
        lines.append("配置画像：")
        for item in (config_profile.get("strong") or [])[:3]:
            kw = item.get("kw") or item.get("tag") or ""
            cnt = item.get("count", 0)
            avg = item.get("avg_ach") or item.get("avg_achievement") or 0
            lines.append(f"  擅长 {kw}：{cnt} 张，均值 {avg}%")
        for item in (config_profile.get("weak") or [])[:2]:
            kw = item.get("kw") or item.get("tag") or ""
            cnt = item.get("count", 0)
            avg = item.get("avg_ach") or item.get("avg_achievement") or 0
            lines.append(f"  短板 {kw}：{cnt} 张，均值 {avg}%")

    config_focus = pack.get("config_focus") or {}
    if config_focus.get("strong") or config_focus.get("weak"):
        lines.append("")
        lines.append("配置切入：")
        for item in (config_focus.get("strong") or [])[:3]:
            lines.append(f"  擅长 {item.get('tag')}：{item.get('count')} 张，均值 {item.get('avg_achievement')}%，同段差距 {item.get('avg_gap_vs_peer')}")
        for item in (config_focus.get("weak") or [])[:2]:
            lines.append(f"  吃瘪 {item.get('tag')}：{item.get('count')} 张，均值 {item.get('avg_achievement')}%，同段差距 {item.get('avg_gap_vs_peer')}")

    b35b15 = pack.get("b35_b15_structure") or {}
    if b35b15:
        lines.append("")
        lines.append("B35/B15：")
        for key in ("b35", "b15"):
            sec = b35b15.get(key) or {}
            if sec:
                lines.append(
                    f"  {key.upper()}：{sec.get('count')} 张，均值 {sec.get('avg_achievement')}%，RA {sec.get('avg_song_rating')}，同段差距 {sec.get('avg_gap_vs_peer')}"
                )

    picked = []
    for key in ("same_rating_average_entry_points", "selected_evidence", "strongest_vs_peer", "highest_song_rating"):
        for c in (pack.get(key) or [])[:3]:
            if c not in picked:
                picked.append(c)
            if len(picked) >= 6:
                break
        if len(picked) >= 6:
            break

    if picked:
        lines.append("")
        lines.append("关键谱：")
        lines.extend(_chart_line(c) for c in picked)

    for label, key in (
        ("同分入口", "same_rating_average_entry_points"),
        ("强证据", "strongest_vs_peer"),
        ("弱证据", "weakest_vs_peer"),
    ):
        rows = pack.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"{label}：")
            for c in rows[:4]:
                pieces = [str(c.get("title") or "")]
                if c.get("ds") is not None:
                    pieces.append(f"定数 {c.get('ds')}")
                if c.get("achievement") is not None:
                    pieces.append(f"达成率 {c.get('achievement'):.4f}%")
                if c.get("song_rating") is not None:
                    pieces.append(f"RA {c.get('song_rating')}")
                if c.get("peer_avg") is not None:
                    pieces.append(f"同段均值 {c.get('peer_avg'):.4f}%")
                if c.get("gap_vs_peer") is not None:
                    pieces.append(f"同段差距 {c.get('gap_vs_peer'):+.4f}")
                if c.get("peer_sample_count") is not None:
                    pieces.append(f"样本 {c.get('peer_sample_count')} 人")
                tag_text = _fmt_tags(c.get("config_tags") or c.get("config") or [])
                if tag_text:
                    pieces.append(f"配置 {tag_text}")
                lines.append("  " + "  ".join(pieces))

    for label, key in (
        ("理论值/高光", "theory_cards"),
        ("15理论", "impossible_15_theory"),
        ("14+AP", "level_14_plus_ap"),
        ("高定数AP", "high_ds_ap"),
        ("异常同段差距", "abnormal_peer_gaps"),
    ):
        rows = pack.get(key) or []
        if rows:
            lines.append("")
            lines.append(f"{label}：")
            for c in rows[:4]:
                pieces = [str(c.get("title") or "")]
                if c.get("ds") is not None:
                    pieces.append(f"定数 {c.get('ds')}")
                if c.get("achievement") is not None:
                    pieces.append(f"达成率 {c.get('achievement'):.4f}%")
                if c.get("song_rating") is not None:
                    pieces.append(f"RA {c.get('song_rating')}")
                if c.get("peer_avg") is not None:
                    pieces.append(f"同段均值 {c.get('peer_avg'):.4f}%")
                if c.get("gap_vs_peer") is not None:
                    pieces.append(f"同段差距 {c.get('gap_vs_peer'):+.4f}")
                lines.append("  " + "  ".join(pieces))

    ds_summary = pack.get("ds_band_summary") or {}
    if ds_summary:
        lines.append("")
        lines.append("定数段：")
        for band in ("<13", "13", "13+", "14", "14+"):
            item = ds_summary.get(band)
            if item:
                lines.append(
                    f"  {band}：均值 {item.get('avg_achievement')}% / 同段差距 {item.get('avg_gap_vs_peer')} / RA {item.get('avg_song_rating')}"
                )

    evidence = pack.get("selected_evidence") or []
    if evidence:
        lines.append("")
        lines.append("核心证据：")
        for c in evidence[:6]:
            pieces = [f"{c.get('title', '')}"]
            if c.get("ds") is not None:
                pieces.append(f"定数 {c.get('ds')}")
            if c.get("achievement") is not None:
                pieces.append(f"达成率 {c.get('achievement'):.4f}%")
            if c.get("song_rating") is not None:
                pieces.append(f"RA {c.get('song_rating')}")
            if c.get("peer_avg") is not None:
                pieces.append(f"同段均值 {c.get('peer_avg'):.4f}%")
            if c.get("gap_vs_peer") is not None:
                pieces.append(f"同段差距 {c.get('gap_vs_peer'):+.4f}")
            tag_text = _fmt_tags(c.get("config_tags") or c.get("config") or [])
            if tag_text:
                pieces.append(f"配置 {tag_text}")
            lines.append("  " + "  ".join(pieces))

    # Push candidates (供给 LLM 选曲)
    push_candidates = context.get("push_candidates") or []
    if push_candidates:
        lines.append("")
        lines.append("推分候选池（已按贴合玩家 B35 定数段筛过，最多选 3 首；不足时少选，禁止跳出此列表另编超纲高难谱）：")
        for i, c in enumerate(push_candidates[:15], 1):
            tag_text = _fmt_tags(c.get("config_tags") or [])
            extra = []
            if c.get("bucket"):
                extra.append(str(c.get("bucket")))
            if c.get("ds_fit") is not None:
                extra.append(f"拟合度{_f(c.get('ds_fit'), 0):.2f}")
            if c.get("ach_gap") is not None:
                extra.append(f"距目标{_f(c.get('ach_gap'), 0):.4f}%")
            if c.get("peer_avg") is not None:
                extra.append(f"同段均值{c.get('peer_avg'):.4f}%")
            if c.get("gap_vs_peer") is not None:
                extra.append(f"同段差距{c.get('gap_vs_peer'):+.4f}")
            if tag_text:
                extra.append(f"配置{tag_text}")
            floor = c.get("replacement_floor")
            entry_text = (
                f"进入{c.get('rating_pool', '')}需替换地板{floor}"
                if floor is not None
                else f"{c.get('rating_pool', '')}当前卡直接提升"
            )
            lines.append(
                f"  {i}. {c.get('title','')}  定数{c.get('ds','')}  达成率{c.get('achievement', c.get('achievements',''))}%  "
                f"目标{c.get('target','')}({c.get('target_achievement','')}%) 预计+{c.get('estimated_gain',0)} RA  "
                f"{entry_text}  {c.get('level_label','')}"
                + (f"  {'  '.join(extra)}" if extra else "")
            )
    else:
        lines.extend([
            "",
            "推分候选池：空。必须明确说暂无可核算的推分路线，禁止推荐任何曲目。",
        ])

    # Chart summaries (community_vibe / chart_identity)
    chart_summaries = context.get("chart_summaries") or {}
    if chart_summaries:
        lines.append("")
        lines.append("大家的评价（community_vibe/chart_identity，自然融入「大家都说」）：")
        summary_rows = []
        summary_seen = set()
        for chart in picked + push_candidates + list(context.get("b50") or []):
            mid = str(chart.get("music_id") or chart.get("song_id") or "")
            if not mid or mid in summary_seen:
                continue
            s = chart_summaries.get(mid)
            if not isinstance(s, dict):
                continue
            summary_seen.add(mid)
            summary_rows.append((str(chart.get("title") or mid), s))
            if len(summary_rows) >= 6:
                break
        for title, s in summary_rows:
            vibe = s.get("community_vibe") or s.get("chart_identity") or ""
            tags = _fmt_tags(s.get("config_tags") or [])
            if vibe or tags:
                lines.append(f"  {title}：{vibe}  配置 {tags}")

    return "\n".join(lines)


async def generate_analysis(
    context: dict, config: Any, style: str = ""
) -> tuple[str, dict[str, Any]]:
    # Keep the system message byte-for-byte stable. User-selected style belongs
    # in the dynamic user message so compatible providers can reuse the much
    # larger system-prefix cache across different users and styles.
    system = _SYSTEM.format(style_instruction="")
    normalized_style = " ".join(str(style or "").split())
    style_instruction = (
        f"\n\n本次表达风格/关注点：{normalized_style}"
        if normalized_style
        else ""
    )

    runtime = await asyncio.to_thread(resolve_llm_runtime_config, config)
    client = AsyncOpenAI(
        api_key=config.b50_llm_key,
        base_url=runtime.base_url,
        timeout=max(1.0, float(getattr(config, "b50_llm_timeout_seconds", 180.0))),
        max_retries=max(0, int(getattr(config, "b50_llm_max_retries", 0))),
    )
    request: dict[str, Any] = dict(
        model=runtime.model,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "本次唯一事实数据如下。只能据此锐评：\n\n"
                    + _fmt(context)
                    + style_instruction
                ),
            },
        ],
        temperature=0.35,
        max_tokens=max(512, int(getattr(config, "b50_llm_max_tokens", 6144))),
        reasoning_effort=_reasoning_effort(config),
    )
    prompt_cache_key = str(
        getattr(config, "b50_llm_prompt_cache_key", "maimaidx-b50-roast-v2") or ""
    ).strip()
    if prompt_cache_key:
        # extra_body keeps compatibility with older openai 1.x clients whose
        # typed create() signature predates prompt_cache_key.
        request["extra_body"] = {"prompt_cache_key": prompt_cache_key}
    try:
        resp = await client.chat.completions.create(**request)
    except BadRequestError as exc:
        detail = str(exc).lower()
        if not prompt_cache_key or not any(
            marker in detail
            for marker in ("prompt_cache_key", "unknown field", "unknown parameter")
        ):
            raise
        log.warning(
            "[b50_analysis] 当前兼容网关不支持 prompt_cache_key，已回退普通请求"
        )
        request.pop("extra_body", None)
        resp = await client.chat.completions.create(**request)
    token_usage = _response_token_usage(resp)
    cached_input_tokens = int(token_usage.get("cached_input_tokens") or 0)
    input_tokens = int(token_usage.get("input_tokens") or 0)
    cache_rate = cached_input_tokens / input_tokens if input_tokens > 0 else 0.0
    log.info(
        "[b50_analysis] 模型 Prompt Cache "
        f"cached={cached_input_tokens} input={input_tokens} rate={cache_rate:.1%}"
    )
    choice = resp.choices[0]
    content = str(_message_field(choice.message, "content") or "").strip()
    finish_reason = _finish_reason(resp)
    reasoning_content = str(
        _message_field(choice.message, "reasoning_content") or ""
    )
    if finish_reason in {"length", "max_tokens"} or not content:
        log.warning(
            "[b50_analysis] LLM 响应不完整 "
            f"model={runtime.model} finish_reason={finish_reason or 'unknown'} "
            f"content_chars={len(content)} reasoning_chars={len(reasoning_content)} "
            f"output_tokens={token_usage.get('output_tokens', 0)}"
        )
    if finish_reason in {"length", "max_tokens"}:
        raise ValueError("模型锐评输出被截断，请稍后重试")

    cleaned = _validated_analysis_payload(content)
    fallback_push = _select_push_recommendations(
        context.get("push_candidates") or [],
        context.get("config_focus") or {},
        normalized_style,
        3,
    )
    cleaned["push_recommendations"] = _merge_push_recommendations(
        cleaned.get("push_recommendations") or [],
        fallback_push,
    )
    return json.dumps(cleaned, ensure_ascii=False), token_usage
