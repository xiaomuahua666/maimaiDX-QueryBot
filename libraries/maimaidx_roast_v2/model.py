from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI, BadRequestError

from ...config import maiconfig
from .analysis import build_report_fallback
from .domain import EvidencePack, RoastReport, StyleSpec
from .policy import validate_report_text
from .prompt import SYSTEM_PROMPT, build_user_prompt


def _clean_report(raw: Any, pack: EvidencePack, style: StyleSpec) -> RoastReport:
    if not isinstance(raw, dict):
        raise ValueError("模型返回格式无效")
    allowed = {"headline", "summary", "strengths", "weaknesses", "actions", "recommendations", "claims"}
    data = {key: raw.get(key) for key in allowed}
    all_text = json.dumps(data, ensure_ascii=False)
    unsupported_high_claim = re.search(r"14\+\s*(?:平均|均值|断层|上限|底力)", all_text)
    if not pack.metrics.get("high_count") and unsupported_high_claim:
        raise ValueError("模型引用了不存在的 14+ 样本")
    text_fields = ("headline", "summary")
    for key in text_fields:
        value = str(data.get(key) or "").strip()
        if not value or not validate_report_text(value)["safe"]:
            raise ValueError("模型返回包含不安全内容")
        data[key] = value[:300]
    lists = {}
    for key in ("strengths", "weaknesses", "actions"):
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
    known = {c.song_id: c for c in pack.candidates}
    recommendations = []
    for item in data.get("recommendations") if isinstance(data.get("recommendations"), list) else []:
        if not isinstance(item, dict) or str(item.get("song_id") or "") not in known:
            continue
        if not validate_report_text(str(item.get("reason") or ""))["safe"]:
            continue
        c = known[str(item["song_id"])]
        recommendations.append({
            "song_id": c.song_id,
            "title": c.title,
            "level": c.level,
            "target": c.target,
            "reason": str(item.get("reason") or c.reason)[:100],
            "cover_path": c.cover_path,
            "artist": c.artist,
            "genre": c.genre,
            "estimated_gain": c.estimated_gain,
            "level_index": c.level_index,
            "chart_type": c.chart_type,
            "pool": c.pool,
            "target_achievement": c.target_achievement,
            "current_ra": c.current_ra,
            "target_ra": c.target_ra,
            "priority_score": c.priority_score,
        })
    claims = []
    evidence_ids = {e.evidence_id for e in pack.evidence}
    for item in data.get("claims") if isinstance(data.get("claims"), list) else []:
        if not isinstance(item, dict):
            continue
        claim = str(item.get("text") or "").strip()
        refs = [str(x) for x in item.get("evidence_ids", []) if str(x) in evidence_ids]
        if claim and refs and validate_report_text(claim)["safe"]:
            claims.append({"text": claim[:160], "evidence_ids": refs[:4]})
    if not claims:
        raise ValueError("模型没有返回可验证的事实依据")
    return RoastReport(data["headline"], data["summary"], lists["strengths"], lists["weaknesses"], lists["actions"], recommendations, claims, style)


async def generate_report(pack: EvidencePack, style: StyleSpec) -> RoastReport:
    if not getattr(maiconfig, "b50_llm_key", ""):
        return build_report_fallback(pack, style)
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
        max_tokens=min(4096, max(512, int(getattr(maiconfig, "b50_llm_max_tokens", 4096)))),
        response_format={"type": "json_object"},
    )
    try:
        response = await client.chat.completions.create(**request)
    except BadRequestError as exc:
        detail = str(exc).lower()
        if "response_format" not in detail and "json_object" not in detail:
            raise
        request.pop("response_format", None)
        response = await client.chat.completions.create(**request)
    content = str(response.choices[0].message.content or "").strip()
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("模型没有返回合法 JSON") from exc
    return _clean_report(payload, pack, style)
