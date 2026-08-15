#!/usr/bin/env python3
"""锐评只能引用真实候选；鸟加及以上不能作为推分推荐。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path = [item for item in sys.path if item and Path(item).resolve() != ROOT]
sys.path.insert(0, str(ROOT.parent))
os.environ.setdefault("COMMAND_START", '["", "/"]')
os.environ.setdefault("MAIMAIDXPATH", str(ROOT / "static"))
os.environ.setdefault("MAIMAIDX_STORAGE_FAIL_FAST", "false")
os.environ.setdefault("MAIMAIDX_MESSAGE_STATS_ENABLED", "false")
os.environ.setdefault("MAIMAIDX_BUSY_SURCHARGE_ENABLED", "false")

import nonebot

nonebot.init()

from nonebot_plugin_maimaidx.libraries.b50_analysis import llm  # noqa: E402


def candidate(
    music_id: str,
    title: str,
    achievement: float,
    *,
    ds: float = 13.4,
    gain: int = 8,
) -> dict:
    return {
        "music_id": music_id,
        "song_id": music_id,
        "level_index": 3,
        "title": title,
        "ds": ds,
        "achievement": achievement,
        "achievements": achievement,
        "target": "SSS+",
        "target_achievement": 100.5,
        "estimated_gain": gain,
        "gain_100": 3,
        "gain_1005": gain,
        "strategy_tag": "综合推荐",
        "reason": "真实候选",
    }


real = candidate("1001", "真实推分曲", 99.8)
at_cap = candidate("1002", "已鸟加曲", 100.5)
theory = candidate("1003", "理论值曲", 101.0)

# 三层边界的第二层：选择器即使收到未过滤输入也只保留 100.5 以下。
selected = llm._select_push_recommendations(
    [at_cap, theory, real], {}, "", limit=4,
)
assert [row["title"] for row in selected] == ["真实推分曲"]

# 即使调用方传入更大 limit，程序兜底也只能输出 3 首。
many_candidates = [candidate(str(2000 + index), f"候选曲{index}", 99.0) for index in range(5)]
assert len(llm._select_push_recommendations(many_candidates, {}, "", limit=99)) == 3
assert len(llm._merge_push_recommendations([], many_candidates)) == 3

# 空响应或合法 JSON 中缺失正文都必须中止，不能继续渲染空白锐评图。
for invalid_response in (
    "",
    '{"title":"B50锐评","overall_roast":"","impression_roast":"",'
    '"push_recommendations":[]}',
    '{"title":"B50锐评","push_recommendations":[]}',
):
    try:
        llm._validated_analysis_payload(invalid_response)
    except ValueError:
        pass
    else:
        raise AssertionError("空锐评正文必须被拒绝")

plain_payload = llm._validated_analysis_payload("这是一段可正常渲染的锐评正文")
assert plain_payload["overall_roast"] == "这是一段可正常渲染的锐评正文"
json_payload = llm._validated_analysis_payload(
    json.dumps(
        {
            "title": "舞萌锐评测试",
            "overall_roast": "正文存在",
            "impression_roast": "总结存在",
            "push_recommendations": [],
        },
        ensure_ascii=False,
    )
)
assert json_payload["overall_roast"] == "正文存在"

# 模型只能选真实候选和改理由，不能用自己的完整字段注入虚构曲目，
# 也不能覆盖后端给出的达成率、定数和收益。
merged = llm._merge_push_recommendations(
    [
        {
            "music_id": "1001",
            "level_index": 3,
            "title": "真实推分曲",
            "ds": 15.0,
            "achievement": 101.0,
            "estimated_gain": 999,
            "strategy_tag": "综合推荐",
            "reason": "模型只可改这句理由",
        },
        {
            "music_id": "9999",
            "level_index": 4,
            "title": "模型虚构曲",
            "ds": 15.0,
            "achievement": 97.0,
            "estimated_gain": 999,
            "reason": "不存在也不能混入",
        },
    ],
    [real, at_cap, theory],
)
assert len(merged) == 1
row = merged[0]
assert row["title"] == "真实推分曲"
assert row["ds"] == 13.4
assert row["achievement"] == 99.8
assert row["estimated_gain"] == 8
assert row["reason"] == "模型只可改这句理由"

# 提示词与调用参数也必须保持闭集、SSS+ 封顶和低随机性约束。
source = (ROOT / "libraries" / "b50_analysis" / "llm.py").read_text(
    encoding="utf-8"
)
assert "【最高优先级：事实闭集】" in llm._SYSTEM
assert "达到 100.5% 后，该谱 rating 已封顶" in llm._SYSTEM
assert "只能从“推分候选池”原样选择曲名" in llm._SYSTEM
assert "普通版严格控制在 450-650 个汉字" in llm._SYSTEM
assert "短版时严格控制在 250-350 个汉字" in llm._SYSTEM
assert "至少使用 6 对" in llm._SYSTEM
assert "push_recommendations 最多 3 首" in llm._SYSTEM
assert "reason 控制在 12-20 个汉字" in llm._SYSTEM
assert "不得继续扩写" in llm._SYSTEM
assert '{"role": "system", "content": system}' in source
assert "temperature=0.35" in source
assert 'getattr(config, "b50_llm_max_tokens", 6144)' in source

print("b50 analysis grounding tests: ok")
