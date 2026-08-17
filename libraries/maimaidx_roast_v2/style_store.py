from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from ...config import Root
from .domain import StyleSpec
from .policy import normalize_style

_PATH = Root / "data" / "roast_v2" / "styles.json"
_LOCK = threading.RLock()


def _read() -> dict[str, str]:
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write(values: dict[str, str]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="styles-", suffix=".json", dir=str(_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(values, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, _PATH)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def get_style(user_id: str) -> StyleSpec:
    with _LOCK:
        return normalize_style(_read().get(str(user_id), ""))


def set_style(user_id: str, raw: str) -> StyleSpec:
    style = normalize_style(raw)
    with _LOCK:
        values = _read()
        values[str(user_id)] = style.raw
        _write(values)
    return style


def clear_style(user_id: str) -> None:
    with _LOCK:
        values = _read()
        values.pop(str(user_id), None)
        _write(values)
