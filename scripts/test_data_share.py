"""数据共享 opt-out 与协议命令文案回归（无需启动 NoneBot）。"""

from __future__ import annotations

import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap() -> None:
    pkg_name = "nonebot_plugin_maimaidx"
    if pkg_name in sys.modules:
        return
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT)]
    sys.modules[pkg_name] = pkg
    lib = types.ModuleType(f"{pkg_name}.libraries")
    lib.__path__ = [str(ROOT / "libraries")]
    sys.modules[f"{pkg_name}.libraries"] = lib


_bootstrap()

from nonebot_plugin_maimaidx.libraries.maimaidx_data_share import DataShareManager  # noqa: E402


def test_default_on_and_opt_out() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "data_share_config.json"
        mgr = DataShareManager(path)
        assert mgr.is_sharing_enabled(10001) is True
        assert mgr.opt_out(10001) is True
        assert mgr.is_sharing_enabled(10001) is False
        assert mgr.opt_out(10001) is False  # 幂等
        assert "10001" in mgr.list_opted_out()
        assert mgr.opt_in(10001) is True
        assert mgr.is_sharing_enabled(10001) is True
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data.get("opted_out_users") == []


def test_agreement_commands_registered() -> None:
    src = (ROOT / "command" / "mai_agreement.py").read_text(encoding="utf-8")
    assert "不同意共享我的数据" in src
    assert "同意共享我的数据" in src
    assert "数据共享状态" in src
    assert "脱敏后作为公开数据" in src


def test_arpi_bucket_and_trend_helpers() -> None:
    import importlib.util

    # 直接加载 context_builder，避免经 __init__ 拉起 openai 依赖
    path = ROOT / "libraries" / "b50_analysis" / "context_builder.py"
    spec = importlib.util.spec_from_file_location(
        f"cb_mod_{id(path)}", path
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    bucket = {
        "arpi_distribution": {
            "count": 40,
            "mean": 0.1,
            "median": 0.0,
            "p25": -0.2,
            "p75": 0.3,
        }
    }
    stats = mod._arpi_bucket_stats(bucket, 0.5)
    assert stats["sufficient"] is True
    assert stats["position"] == "above_p75"
    low = mod._arpi_bucket_stats(bucket, -0.5)
    assert low["position"] == "below_p25"
    thin = mod._arpi_bucket_stats({"arpi_distribution": {"count": 3, "median": 0}}, 0.1)
    assert thin["sufficient"] is False

    trend = mod._normalize_rating_trend(
        {
            "points": [
                {"date": f"2026-07-{i:02d}", "rating": 15000 + i}
                for i in range(1, 12)
            ]
        }
    )
    assert trend["delta"] == 10
    assert "稳步上升" in trend["feasibility_hint"]
    fast = mod._normalize_rating_trend(
        {
            "points": [
                {"date": "2026-07-01", "rating": 15000},
                {"date": "2026-07-02", "rating": 15100},
            ]
        }
    )
    assert "进攻" in fast["feasibility_hint"]


def test_export_script_exists() -> None:
    path = ROOT / "scripts" / "export_public_dataset.py"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "peer_stats" in text
    assert "opted_out" in text
    assert "roast_training_samples" in text


if __name__ == "__main__":
    test_default_on_and_opt_out()
    test_agreement_commands_registered()
    test_export_script_exists()
    test_arpi_bucket_and_trend_helpers()
    print("ok")
