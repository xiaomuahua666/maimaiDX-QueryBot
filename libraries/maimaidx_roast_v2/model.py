from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger as log
from openai import AsyncOpenAI, BadRequestError

from ...config import maiconfig
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
    if usage is None:
        return {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    input_tokens = _field(usage, "prompt_tokens", "input_tokens")
    output_tokens = _field(usage, "completion_tokens", "output_tokens")
    total_tokens = _field(usage, "total_tokens")
    prompt_details = _field(usage, "prompt_tokens_details", "input_tokens_details")
    cached = _field(prompt_details, "cached_tokens") if prompt_details is not None else 0
    try:
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        total_tokens = int(total_tokens or 0)
        cached = int(cached or 0)
    except (TypeError, ValueError):
        return {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    available = input_tokens > 0 or output_tokens > 0
    return {
        "available": available,
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens),
        "cached_input_tokens": max(0, min(cached, input_tokens)),
    }


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


def _clean_report(raw: Any, pack: EvidencePack, style: StyleSpec) -> RoastReport:
    if not isinstance(raw, dict):
        raise ValueError("模型返回格式无效")
    allowed = {
        "headline", "summary", "analysis", "strengths", "weaknesses",
        "peer_takeaways", "actions", "recommendations", "claims",
    }
    data = {key: raw.get(key) for key in allowed}
    all_text = json.dumps(data, ensure_ascii=False)
    unsupported_high_claim = re.search(r"14\+\s*(?:平均|均值|断层|上限|底力)", all_text)
    if not pack.metrics.get("high_count") and unsupported_high_claim:
        raise ValueError("模型引用了不存在的 14+ 样本")
    for key, limit in (("headline", 120), ("summary", 1000), ("analysis", 2600)):
        value = str(data.get(key) or "").strip()
        if not value or not validate_report_text(value)["safe"]:
            raise ValueError("模型返回包含不安全内容")
        data[key] = value[:limit]
    lists = {}
    for key in ("strengths", "weaknesses", "peer_takeaways", "actions"):
        values = data.get(key) if isinstance(data.get(key), list) else []
        cleaned_values = []
        for item in values[:5]:
            value = str(item).strip()[:120]
            if not value:
                continue
            if not validate_report_text(value)["safe"]:
                raise ValueError("模型返回包含不安全内容")
            cleaned_values.append(value)
        lists[key] = cleaned_values
    if not lists["peer_takeaways"]:
        raise ValueError("模型没有返回同段结论")

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
        reason = str(item.get("reason") or candidate.reason or "").strip()
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
        claim = str(item.get("text") or "").strip()
        refs = [str(x) for x in item.get("evidence_ids", []) if str(x) in evidence_ids]
        if claim and refs and validate_report_text(claim)["safe"]:
            claims.append({"text": claim[:160], "evidence_ids": refs[:4]})
    if not claims:
        raise ValueError("模型没有返回可验证的事实依据")
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
    )


async def generate_report(pack: EvidencePack, style: StyleSpec) -> tuple[RoastReport, dict[str, Any]]:
    if not getattr(maiconfig, "b50_llm_key", ""):
        return build_report_fallback(pack, style), {"available": False, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    client = AsyncOpenAI(
        api_key=maiconfig.b50_llm_key,
        base_url=str(maiconfig.b50_llm_url).rstrip("/"),
        timeout=max(1.0, float(getattr(maiconfig, "b50_llm_timeout_seconds", 180.0))),
        max_retries=0,
    )
    request = dict(
        model=maiconfig.b50_llm_model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": build_user_prompt(pack, style)}],
        temperature=0.35,
        max_tokens=max(512, int(getattr(maiconfig, "b50_llm_max_tokens", 6144))),
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
    for _ in range(3):
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
            raise
    if response is None:
        raise RuntimeError("模型请求未返回响应")
    usage = _token_usage(response)
    cached_input_tokens = int(usage.get("cached_input_tokens") or 0)
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_rate = cached_input_tokens / input_tokens if input_tokens > 0 else 0.0
    log.info(
        "[roast_v2] 模型 Prompt Cache "
        f"cached={cached_input_tokens} input={input_tokens} rate={cache_rate:.1%}"
    )
    content = str(response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        error = ValueError("模型没有返回合法 JSON")
        error.token_usage = usage
        raise error from exc
    try:
        report = _clean_report(payload, pack, style)
    except Exception as exc:
        exc.token_usage = usage
        raise
    return report, usage
