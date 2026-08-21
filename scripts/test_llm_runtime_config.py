#!/usr/bin/env python3

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries.maimaidx_llm_runtime import (
    LLM_MODEL_OVERRIDE_KEY,
    LLM_URL_OVERRIDE_KEY,
    resolve_llm_runtime_config,
    set_llm_runtime_config,
)


class FakeDb:
    def __init__(self, values=None, *, fail_reads: bool = False):
        self.values = dict(values or {})
        self.fail_reads = fail_reads

    def get_config(self, key: str, default: str = "") -> str:
        if self.fail_reads:
            raise RuntimeError("database unavailable")
        return self.values.get(key, default)

    def set_config(self, key: str, value: str) -> None:
        self.values[key] = value


CONFIG = SimpleNamespace(
    b50_llm_url="https://env.example/v1/",
    b50_llm_model="env-model",
)


def test_empty_overrides_use_environment() -> None:
    runtime = resolve_llm_runtime_config(CONFIG, FakeDb())
    assert runtime.base_url == "https://env.example/v1"
    assert runtime.model == "env-model"
    assert runtime.url_source == "environment"
    assert runtime.model_source == "environment"


def test_database_overrides_take_effect_immediately() -> None:
    db = FakeDb({
        LLM_URL_OVERRIDE_KEY: "https://db.example/openai/v1/",
        LLM_MODEL_OVERRIDE_KEY: "qwen3.8-27b",
    })
    runtime = resolve_llm_runtime_config(CONFIG, db)
    assert runtime.base_url == "https://db.example/openai/v1"
    assert runtime.model == "qwen3.8-27b"
    assert runtime.url_source == "database"
    assert runtime.model_source == "database"


def test_set_and_reset_runtime_config() -> None:
    db = FakeDb()
    runtime = set_llm_runtime_config(
        base_url="https://hot.example/v1/",
        model="new/model-v2",
        config=CONFIG,
        db=db,
    )
    assert runtime.base_url == "https://hot.example/v1"
    assert runtime.model == "new/model-v2"
    assert db.values[LLM_URL_OVERRIDE_KEY] == "https://hot.example/v1"
    assert db.values[LLM_MODEL_OVERRIDE_KEY] == "new/model-v2"

    runtime = set_llm_runtime_config(reset=True, config=CONFIG, db=db)
    assert runtime.base_url == "https://env.example/v1"
    assert runtime.model == "env-model"
    assert db.values[LLM_URL_OVERRIDE_KEY] == ""
    assert db.values[LLM_MODEL_OVERRIDE_KEY] == ""


def test_invalid_values_are_rejected_before_write() -> None:
    invalid_values = (
        {"base_url": "ftp://example.com/v1"},
        {"base_url": "https://user:secret@example.com/v1"},
        {"base_url": "https://example.com/v1?token=secret"},
        {"model": "model with spaces"},
    )
    for values in invalid_values:
        db = FakeDb()
        try:
            set_llm_runtime_config(config=CONFIG, db=db, **values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid config accepted: {values}")
        assert not db.values


def test_database_read_failure_falls_back_to_environment() -> None:
    runtime = resolve_llm_runtime_config(CONFIG, FakeDb(fail_reads=True))
    assert runtime.base_url == "https://env.example/v1"
    assert runtime.model == "env-model"


def test_invalid_database_values_fall_back_to_environment() -> None:
    db = FakeDb({
        LLM_URL_OVERRIDE_KEY: "javascript:alert(1)",
        LLM_MODEL_OVERRIDE_KEY: "bad model",
    })
    runtime = resolve_llm_runtime_config(CONFIG, db)
    assert runtime.base_url == "https://env.example/v1"
    assert runtime.model == "env-model"
    assert runtime.url_source == "environment"
    assert runtime.model_source == "environment"


def main() -> None:
    test_empty_overrides_use_environment()
    test_database_overrides_take_effect_immediately()
    test_set_and_reset_runtime_config()
    test_invalid_values_are_rejected_before_write()
    test_database_read_failure_falls_back_to_environment()
    test_invalid_database_values_fall_back_to_environment()
    print("llm runtime config tests passed")


if __name__ == "__main__":
    main()
