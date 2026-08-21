"""Database-backed runtime overrides for the shared B50 LLM endpoint."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from ..config import log

LLM_URL_OVERRIDE_KEY = "b50_llm_url_override"
LLM_MODEL_OVERRIDE_KEY = "b50_llm_model_override"

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/+\-]+$")


@dataclass(frozen=True)
class LlmRuntimeConfig:
    base_url: str
    model: str
    url_source: str
    model_source: str


def _default_config() -> Any:
    from ..config import maiconfig

    return maiconfig


def _default_db() -> Any:
    from .maimaidx_break import break_db

    return break_db


def validate_llm_base_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 500:
        raise ValueError("Base URL 不能为空，且长度不能超过 500 个字符")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Base URL 必须是完整的 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise ValueError("Base URL 不允许包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("Base URL 不允许包含查询参数或片段")
    normalized = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", "")
    )
    return normalized.rstrip("/")


def validate_llm_model(value: str) -> str:
    model = str(value or "").strip()
    if not model or len(model) > 128:
        raise ValueError("模型名不能为空，且长度不能超过 128 个字符")
    if not _MODEL_RE.fullmatch(model):
        raise ValueError("模型名只能包含字母、数字及 . _ : / + -")
    return model


def resolve_llm_runtime_config(
    config: Any = None,
    db: Any = None,
) -> LlmRuntimeConfig:
    """Resolve DB overrides for one request, falling back safely to env config."""
    config = config or _default_config()
    fallback_url = validate_llm_base_url(getattr(config, "b50_llm_url", ""))
    fallback_model = validate_llm_model(getattr(config, "b50_llm_model", ""))
    db = db or _default_db()
    try:
        url_override = str(db.get_config(LLM_URL_OVERRIDE_KEY, "") or "").strip()
        model_override = str(db.get_config(LLM_MODEL_OVERRIDE_KEY, "") or "").strip()
    except Exception as exc:
        log.warning(
            "[LLM配置] 读取数据库覆盖值失败，已回退环境配置："
            f"{type(exc).__name__}: {exc}"
        )
        url_override = ""
        model_override = ""

    try:
        base_url = validate_llm_base_url(url_override) if url_override else fallback_url
    except ValueError as exc:
        log.warning(f"[LLM配置] 数据库 Base URL 无效，已回退环境配置：{exc}")
        base_url = fallback_url
        url_override = ""
    try:
        model = validate_llm_model(model_override) if model_override else fallback_model
    except ValueError as exc:
        log.warning(f"[LLM配置] 数据库模型名无效，已回退环境配置：{exc}")
        model = fallback_model
        model_override = ""

    return LlmRuntimeConfig(
        base_url=base_url,
        model=model,
        url_source="database" if url_override else "environment",
        model_source="database" if model_override else "environment",
    )


def set_llm_runtime_config(
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    reset: bool = False,
    config: Any = None,
    db: Any = None,
) -> LlmRuntimeConfig:
    """Validate and persist runtime overrides, then return effective values."""
    config = config or _default_config()
    db = db or _default_db()
    if reset:
        db.set_config(LLM_URL_OVERRIDE_KEY, "")
        db.set_config(LLM_MODEL_OVERRIDE_KEY, "")
    else:
        if base_url is None and model is None:
            raise ValueError("请至少提供 Base URL 或模型名")
        normalized_url = validate_llm_base_url(base_url) if base_url is not None else None
        normalized_model = validate_llm_model(model) if model is not None else None
        if normalized_url is not None:
            db.set_config(LLM_URL_OVERRIDE_KEY, normalized_url)
        if normalized_model is not None:
            db.set_config(LLM_MODEL_OVERRIDE_KEY, normalized_model)
    return resolve_llm_runtime_config(config=config, db=db)
