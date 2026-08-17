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

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries.b50_analysis import llm  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_roast_v2 import model as roast_model  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_roast_v2.domain import (  # noqa: E402
    Evidence,
    EvidencePack,
    StyleSpec,
)


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


roast_requests: list[dict] = []


class RoastFakeCompletions:
    async def create(self, **kwargs):
        roast_requests.append(kwargs)
        return SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=2000,
                completion_tokens=300,
                total_tokens=2300,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1500),
            ),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"headline":"稳定前缀测试",'
                            '"summary":"当前成绩结构稳定。",'
                            '"analysis":"当前成绩结构稳定，建议继续整理地板。",'
                            '"strengths":["当前数据有稳定表现"],'
                            '"weaknesses":["仍有地板整理空间"],'
                            '"peer_takeaways":["同段样本不足"],'
                            '"actions":["完成一首后重新生成报告"],'
                            '"recommendations":[],'
                            '"claims":[{"text":"当前 Rating 为 15000",'
                            '"evidence_ids":["rating"]}]}'
                        )
                    )
                )
            ],
        )


class RoastFakeClient:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=RoastFakeCompletions())


async def roast_main() -> None:
    original_client = roast_model.AsyncOpenAI
    original_config = roast_model.maiconfig
    roast_model.AsyncOpenAI = RoastFakeClient
    roast_model.maiconfig = SimpleNamespace(
        b50_llm_key="test",
        b50_llm_url="https://example.invalid/v1",
        b50_llm_model="cache-test-model",
        b50_llm_timeout_seconds=10,
        b50_llm_max_tokens=1024,
        b50_llm_prompt_cache_key="maimaidx-b50-roast-v2",
    )
    pack = EvidencePack(
        nickname="Milk",
        rating=15000,
        evidence=[Evidence("rating", "当前 Rating", "15000", "snapshot")],
        metrics={"high_count": 0},
    )
    try:
        _, usage = await roast_model.generate_report(
            pack, StyleSpec(direction="短版直说")
        )
        await roast_model.generate_report(pack, StyleSpec(direction="温柔一点"))
    finally:
        roast_model.AsyncOpenAI = original_client
        roast_model.maiconfig = original_config

    assert len(roast_requests) == 2
    first, second = roast_requests
    assert first["extra_body"]["prompt_cache_key"] == "maimaidx-b50-roast-v2"
    assert first["messages"][0]["content"] == second["messages"][0]["content"]
    assert (
        first["messages"][1]["content"].split("\nSTYLE_JSON:\n", 1)[0]
        == second["messages"][1]["content"].split("\nSTYLE_JSON:\n", 1)[0]
    )
    assert usage["cached_input_tokens"] == 1500


asyncio.run(roast_main())
print("b50 prompt cache tests: ok")
