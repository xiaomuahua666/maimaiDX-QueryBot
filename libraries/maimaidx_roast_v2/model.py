from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from loguru import logger as log
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    BadRequestError,
)

from ...config import maiconfig
from ..maimaidx_break import analysis_reasoning_effort
from ..maimaidx_llm_runtime import resolve_llm_runtime_config
from .analysis import build_report_fallback
from .domain import EvidencePack, RoastReport, StyleSpec
from .policy import validate_report_text
from .prompt import SYSTEM_PROMPT, build_user_prompt, evidence_ids_for_pack


def _field(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _token_usage(response: Any) -> dict[str, Any]:
    usage = _field(response, "usage")
    if usage is None and isinstance(response, dict):
        # Some OneAPI deployments wrap the Chat Completions envelope in
        # ``data``/``response`` while leaving choices at the outer level.
        for wrapper in ("data", "response", "meta"):
            nested = response.get(wrapper)
            if isinstance(nested, dict) and nested.get("usage") is not None:
                usage = nested.get("usage")
                break
    if usage is None:
        return {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    input_tokens = _field(usage, "prompt_tokens", "input_tokens", "input_token_count")
    output_tokens = _field(usage, "completion_tokens", "output_tokens", "output_token_count")
    total_tokens = _field(usage, "total_tokens")
    prompt_details = _field(usage, "prompt_tokens_details", "input_tokens_details")
    cached = _field(prompt_details, "cached_tokens") if prompt_details is not None else _field(usage, "cached_tokens")
    try:
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        total_tokens = int(total_tokens or 0)
        cached = int(cached or 0)
    except (TypeError, ValueError):
        return {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    available = input_tokens > 0 or output_tokens > 0 or total_tokens > 0
    return {
        "available": available,
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
        "cached_input_tokens": max(0, min(cached, input_tokens)),
    }


def _normalize_response(response: Any) -> Any:
    """解包部分 OneAPI 网关返回的字符串或 data 包裹 JSON。"""
    current = response
    for _ in range(3):
        if isinstance(current, str):
            text = current.strip()
            if not text:
                return current
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return current
            if parsed == current:
                return current
            current = parsed
            continue
        if isinstance(current, dict):
            data = current.get("data")
            if (
                "choices" not in current
                and isinstance(data, dict)
                and ("choices" in data or "usage" in data)
            ):
                current = data
                continue
        break
    return current


def _response_content(response: Any) -> str:
    choices = _field(response, "choices")
    if choices:
        message = _field(choices[0], "message")
        content = _field(message, "content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, dict):
            value = _field(content, "text", "output_text", "content")
            if value is not None:
                return str(value).strip()
            return json.dumps(content, ensure_ascii=False)
        if isinstance(content, list):
            parts = []
            for item in content:
                value = _field(item, "text", "output_text", "content")
                if value is not None:
                    parts.append(str(value))
            return "".join(parts).strip()
        return str(content or "").strip()
    if isinstance(response, dict) and any(
        key in response for key in ("headline", "summary", "analysis")
    ):
        return json.dumps(response, ensure_ascii=False)
    if isinstance(response, str):
        return response.strip()
    return ""


def _finish_reason(response: Any) -> str:
    choices = _field(response, "choices")
    if not choices:
        return ""
    return str(_field(choices[0], "finish_reason") or "").strip().lower()


def _parse_json_object(content: str) -> dict[str, Any]:
    """解析合法 JSON 对象，并兼容代码围栏或少量前后说明。"""
    text = str(content or "").strip().lstrip("\ufeff")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("模型没有返回合法 JSON")


def _candidate_key(value: Any) -> tuple[str, str, int]:
    """Use the full chart identity so duplicate song IDs cannot cross-match."""
    song_id = str(_field(value, "song_id", "music_id") or "").strip()
    chart_type = str(_field(value, "chart_type", "type") or "SD").upper()
    try:
        level_index = int(_field(value, "level_index") if _field(value, "level_index") is not None else -1)
    except (TypeError, ValueError):
        level_index = -1
    return song_id, chart_type, level_index


def _candidate_row(candidate: Any, reason: str | None = None) -> dict[str, Any]:
    """Copy factual candidate fields; never accept model-supplied numbers."""
    data = candidate.__dict__ if hasattr(candidate, "__dict__") else dict(candidate or {})
    row = {
        "song_id": str(data.get("song_id") or ""),
        "title": str(data.get("title") or ""),
        "level": str(data.get("level") or ""),
        "target": str(data.get("target") or ""),
        "reason": str(reason or data.get("reason") or "")[:120],
        "cover_path": str(data.get("cover_path") or ""),
        "artist": str(data.get("artist") or ""),
        "genre": str(data.get("genre") or ""),
        "estimated_gain": int(data.get("estimated_gain") or 0),
        "level_index": int(data.get("level_index") or 0),
        "chart_type": str(data.get("chart_type") or "SD").upper(),
        "pool": str(data.get("pool") or "old"),
        "target_achievement": data.get("target_achievement"),
        "current_ra": int(data.get("current_ra") or 0),
        "target_ra": int(data.get("target_ra") or 0),
        "priority_score": data.get("priority_score", 0.0),
        "route_step": int(data.get("route_step") or 0),
        "cumulative_gain": int(data.get("cumulative_gain") or 0),
        "risk": str(data.get("risk") or "稳妥"),
    }
    return row


def _has_unsupported_high_claim(text: str) -> bool:
    pattern = re.compile(r"14\+\s*(?:平均|均值|断层|上限|底力)")
    absence_markers = (
        "暂无", "没有", "无样本", "样本不足", "不可用", "无法判断",
        "不能判断", "不作结论", "未提供", "数据缺失", "0首", "为0",
    )
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 24):match.end() + 36]
        if any(marker in window for marker in absence_markers):
            continue
        return True
    return False


_PLAYER_FRIENDLY_TERMS = (
    ("同段聚合参考", "相近 Rating 玩家参考"),
    ("同段聚合", "相近 Rating 玩家数据"),
    ("槽位地板", "B50 里最低的几首"),
    ("coverage", "数据覆盖"),
    ("Coverage", "数据覆盖"),
    ("置信区间", "参考范围"),
    ("置信度", "数据可靠程度"),
    ("方差", "成绩波动"),
    ("P25/P75", "较低/较高参考线"),
    ("ARPI", "同段差距"),
)


def _player_friendly_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    for source, target in _PLAYER_FRIENDLY_TERMS:
        text = text.replace(source, target)
    return text[:limit]


def _clean_report(raw: Any, pack: EvidencePack, style: StyleSpec) -> RoastReport:
    if not isinstance(raw, dict):
        raise ValueError("模型返回格式无效")
    allowed = {
        "headline", "summary", "analysis", "strengths", "weaknesses",
        "peer_takeaways", "actions", "highlights", "score_spotlights",
        "recommendations", "claims",
    }
    data = {key: raw.get(key) for key in allowed}
    all_text = json.dumps(data, ensure_ascii=False)
    if not pack.metrics.get("high_count") and _has_unsupported_high_claim(all_text):
        raise ValueError("模型引用了不存在的 14+ 样本")
    for key, limit in (("headline", 120), ("summary", 1000), ("analysis", 2600)):
        value = _player_friendly_text(data.get(key), limit)
        if not value or not validate_report_text(value)["safe"]:
            raise ValueError("模型返回包含不安全内容")
        data[key] = value[:limit]
    lists = {}
    for key in ("strengths", "weaknesses", "peer_takeaways", "actions"):
        values = data.get(key) if isinstance(data.get(key), list) else []
        cleaned_values = []
        for item in values[:5]:
            value = _player_friendly_text(item, 120)
            if not value:
                continue
            if not validate_report_text(value)["safe"]:
                raise ValueError("模型返回包含不安全内容")
            cleaned_values.append(value)
        lists[key] = cleaned_values
    fallback = None
    if not lists["peer_takeaways"]:
        fallback = build_report_fallback(pack, style)
        lists["peer_takeaways"] = fallback.peer_takeaways[:3]

    known_by_key = {_candidate_key(candidate): candidate for candidate in pack.candidates}
    by_song: dict[str, list[Any]] = {}
    for candidate in pack.candidates:
        by_song.setdefault(str(candidate.song_id), []).append(candidate)
    recommendations = []
    for item in data.get("recommendations") if isinstance(data.get("recommendations"), list) else []:
        if not isinstance(item, dict):
            continue
        key = _candidate_key(item)
        candidate = known_by_key.get(key)
        if candidate is None:
            song_id = str(item.get("song_id") or "").strip()
            matches = by_song.get(song_id) or []
            candidate = matches[0] if len(matches) == 1 else None
        if candidate is None:
            continue
        reason = _player_friendly_text(item.get("reason") or candidate.reason, 120)
        if reason and not validate_report_text(reason)["safe"]:
            continue
        recommendations.append(_candidate_row(candidate, reason))
    selected_ids = {_candidate_key(item) for item in recommendations}
    for candidate in pack.candidates[:5]:
        if _candidate_key(candidate) in selected_ids:
            continue
        recommendations.append(_candidate_row(candidate))
        selected_ids.add(_candidate_key(candidate))
        if len(recommendations) >= 5:
            break
    claims = []
    evidence_ids = evidence_ids_for_pack(pack)
    for item in data.get("claims") if isinstance(data.get("claims"), list) else []:
        if not isinstance(item, dict):
            continue
        claim = _player_friendly_text(item.get("text"), 160)
        refs = [str(x) for x in item.get("evidence_ids", []) if str(x) in evidence_ids]
        if claim and refs and validate_report_text(claim)["safe"]:
            claims.append({"text": claim[:160], "evidence_ids": refs[:4]})
    if not claims:
        fallback = fallback or build_report_fallback(pack, style)
        for item in fallback.claims:
            refs = [str(x) for x in item.get("evidence_ids", []) if str(x) in evidence_ids]
            claim = _player_friendly_text(item.get("text"), 160)
            if claim and refs:
                claims.append({"text": claim[:160], "evidence_ids": refs[:4]})
        if not claims and pack.evidence:
            evidence = pack.evidence[0]
            claims.append({
                "text": f"{evidence.label}：{evidence.value}"[:160],
                "evidence_ids": [evidence.evidence_id],
            })

    fallback = fallback or build_report_fallback(pack, style)
    highlights = []
    allowed_tones = {"positive", "warning", "action", "neutral"}
    for item in data.get("highlights") if isinstance(data.get("highlights"), list) else []:
        if not isinstance(item, dict):
            continue
        title = _player_friendly_text(item.get("title"), 18)
        text = _player_friendly_text(item.get("text"), 320)
        tone = str(item.get("tone") or "neutral").strip().lower()
        refs = [str(value) for value in item.get("evidence_ids", []) if str(value) in evidence_ids]
        if (
            title and text and refs and tone in allowed_tones
            and validate_report_text(title)["safe"]
            and validate_report_text(text)["safe"]
        ):
            highlights.append({
                "title": title,
                "text": text,
                "tone": tone,
                "evidence_ids": refs[:4],
            })
        if len(highlights) >= 3:
            break
    if not highlights:
        highlights = list(fallback.highlights[:3])

    score_spotlights = []
    known_song_ids = {value for value in evidence_ids if value.startswith("song:")}
    seen_song_ids: set[str] = set()
    for item in data.get("score_spotlights") if isinstance(data.get("score_spotlights"), list) else []:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        verdict = _player_friendly_text(item.get("verdict"), 90)
        if (
            evidence_id in known_song_ids and evidence_id not in seen_song_ids
            and verdict and validate_report_text(verdict)["safe"]
        ):
            score_spotlights.append({"evidence_id": evidence_id, "verdict": verdict})
            seen_song_ids.add(evidence_id)
        if len(score_spotlights) >= 4:
            break
    if not score_spotlights:
        score_spotlights = list(fallback.score_spotlights[:4])
    return RoastReport(
        headline=data["headline"],
        summary=data["summary"],
        analysis=data.get("analysis") or data["summary"],
        strengths=lists["strengths"],
        weaknesses=lists["weaknesses"],
        peer_takeaways=lists["peer_takeaways"],
        actions=lists["actions"],
        recommendations=recommendations,
        claims=claims,
        style=style,
        highlights=highlights,
        score_spotlights=score_spotlights,
    )


async def generate_report(pack: EvidencePack, style: StyleSpec) -> tuple[RoastReport, dict[str, Any]]:
    if not getattr(maiconfig, "b50_llm_key", ""):
        raise RuntimeError("锐评模型未配置，无法生成模型报告")
    runtime = await asyncio.to_thread(resolve_llm_runtime_config, maiconfig)
    client = AsyncOpenAI(
        api_key=maiconfig.b50_llm_key,
        base_url=runtime.base_url,
        timeout=max(
            1.0,
            float(getattr(maiconfig, "b50_llm_request_timeout_seconds", 180.0)),
        ),
        max_retries=max(0, int(getattr(maiconfig, "b50_llm_max_retries", 0))),
    )
    reasoning_effort = await asyncio.to_thread(analysis_reasoning_effort)
    request = dict(
        model=runtime.model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": build_user_prompt(pack, style)}],
        temperature=0.35,
        max_tokens=max(512, int(getattr(maiconfig, "b50_llm_max_tokens", 6144))),
        reasoning_effort=reasoning_effort,
        response_format={"type": "json_object"},
    )
    prompt_cache_key = str(
        getattr(maiconfig, "b50_llm_prompt_cache_key", "maimaidx-b50-roast-v2") or ""
    ).strip()
    if prompt_cache_key:
        # OpenAI-compatible gateways can use this stable routing key to keep
        # identical system-prefix blocks in the same prompt-cache partition.
        request["extra_body"] = {"prompt_cache_key": prompt_cache_key}

    response = None
    for _ in range(8):
        try:
            response = await client.chat.completions.create(**request)
            break
        except BadRequestError as exc:
            detail = str(exc).lower()
            if "extra_body" in request and "prompt_cache_key" in detail:
                log.warning("[roast_v2] 当前网关不支持 prompt_cache_key，已回退普通请求")
                request.pop("extra_body", None)
                continue
            if "response_format" in request and any(
                marker in detail for marker in ("response_format", "json_object")
            ):
                request.pop("response_format", None)
                continue
            if "reasoning_effort" in request and any(
                marker in detail for marker in ("reasoning_effort", "reasoning effort")
            ):
                log.warning("[roast_v2] 当前网关不支持 reasoning_effort，已回退默认请求")
                request.pop("reasoning_effort", None)
                continue
            if "extra_body" in request and any(
                marker in detail for marker in ("unknown field", "unknown parameter")
            ):
                log.warning("[roast_v2] 当前网关拒绝扩展缓存参数，已回退普通请求")
                request.pop("extra_body", None)
                continue
            if "response_format" in request and any(
                marker in detail for marker in ("unknown field", "unknown parameter")
            ):
                request.pop("response_format", None)
                continue
            if "reasoning_effort" in request and any(
                marker in detail for marker in ("unknown field", "unknown parameter")
            ):
                log.warning("[roast_v2] 当前网关拒绝 reasoning_effort，已回退默认请求")
                request.pop("reasoning_effort", None)
                continue
        except APIStatusError as exc:
            # A few OneAPI gateways surface unsupported extension fields as a
            # generic 500 instead of a useful 400. Retry once with a smaller
            # compatibility surface; the SDK's bounded retry handles transient
            # upstream failures before reaching this branch.
            status = int(getattr(exc, "status_code", 0) or 0)
            detail = str(exc).lower()
            if status >= 500 and "extra_body" in request:
                log.warning("[roast_v2] 上游 5xx，移除 Prompt Cache 扩展后重试")
                request.pop("extra_body", None)
                continue
            if status >= 500 and "reasoning_effort" in request:
                log.warning("[roast_v2] 上游 5xx，移除 reasoning_effort 后重试")
                request.pop("reasoning_effort", None)
                continue
            if status >= 500 and "response_format" in request:
                log.warning("[roast_v2] 上游 5xx，移除 response_format 后重试")
                request.pop("response_format", None)
                continue
            if status >= 500 and any(marker in detail for marker in ("do_request_failed", "upstream error")):
                log.warning("[roast_v2] 上游 5xx 重试后仍失败")
            raise
        except (APITimeoutError, APIConnectionError) as exc:
            # Transport failures are unrelated to optional request fields.
            # Retrying each compatibility variant can keep a user waiting for
            # several minutes and eventually expire the official-QQ msgid.
            log.warning(f"[roast_v2] {type(exc).__name__}，结束本次请求")
            raise
    if response is None:
        raise RuntimeError("模型请求未返回响应")
    raw_response = response
    response = _normalize_response(response)
    if isinstance(raw_response, str) and not isinstance(response, str):
        log.info("[roast_v2] 已兼容解包 OneAPI 字符串响应")
    usage = _token_usage(response)
    if not usage.get("available"):
        error = ValueError("模型未返回 Token 用量，本次报告不发送且不扣费")
        error.token_usage = usage
        raise error
    cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_rate = cached_input_tokens / input_tokens if input_tokens > 0 else 0.0
    log.info(
        "[roast_v2] 模型 Prompt Cache "
        f"cached={cached_input_tokens} input={input_tokens} rate={cache_rate:.1%}"
    )
    content = _response_content(response)
    if not content:
        finish_reason = _finish_reason(response)
        log.warning(
            "[roast_v2] 模型正文为空 "
            f"model={runtime.model} "
            f"finish_reason={finish_reason or 'unknown'} "
            f"output_tokens={usage.get('output_tokens', 0)}"
        )
        error = ValueError("模型未返回锐评正文，请检查上游状态或输出预算")
        error.token_usage = usage
        raise error
    try:
        payload = _parse_json_object(content)
    except ValueError as exc:
        error = ValueError(str(exc))
        error.token_usage = usage
        raise error from exc
    try:
        report = _clean_report(payload, pack, style)
    except Exception as exc:
        exc.token_usage = usage
        raise
    return report, usage
