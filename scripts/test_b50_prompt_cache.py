"""B50 model requests keep a stable prefix for provider Prompt Cache hits."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))

from nonebot_plugin_maimaidx.libraries.b50_analysis import llm  # noqa: E402


requests: list[dict] = []


class FakeCompletions:
    async def create(self, **kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=2000,
                completion_tokens=300,
                total_tokens=2300,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1500),
            ),
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=(
                            '{"title":"缓存锐评测试",'
                            '"overall_roast":"这是稳定前缀测试正文",'
                            '"impression_roast":"缓存正常",'
                            '"push_recommendations":[]}'
                        ),
                        reasoning_content="",
                    ),
                )
            ],
        )


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=FakeCompletions())


async def main() -> None:
    original = llm.AsyncOpenAI
    llm.AsyncOpenAI = FakeClient
    config = SimpleNamespace(
        b50_llm_key="test",
        b50_llm_url="https://example.invalid/v1",
        b50_llm_model="cache-test-model",
        b50_llm_timeout_seconds=10,
        b50_llm_max_retries=0,
        b50_llm_max_tokens=1024,
        b50_llm_reasoning_effort="low",
        b50_llm_prompt_cache_key="maimaidx-b50-roast-v2",
    )
    context = {"player": {"nickname": "Milk", "rating": 15000}}
    try:
        _, usage_a = await llm.generate_analysis(context, config, "  短版   直说  ")
        await llm.generate_analysis(context, config, "温柔一点")
    finally:
        llm.AsyncOpenAI = original

    assert len(requests) == 2
    first, second = requests
    assert first["extra_body"]["prompt_cache_key"] == "maimaidx-b50-roast-v2"
    assert first["messages"][0]["content"] == second["messages"][0]["content"]
    assert "短版 直说" not in first["messages"][0]["content"]
    assert first["messages"][1]["content"].endswith("本次表达风格/关注点：短版 直说")
    assert usage_a["cached_input_tokens"] == 1500


asyncio.run(main())
print("b50 prompt cache tests: ok")
