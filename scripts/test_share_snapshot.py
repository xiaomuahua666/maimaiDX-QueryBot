"""贡献快照与延迟 mkdir 回归测试（无需 NoneBot）。"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap() -> None:
    pkg = "nonebot_plugin_maimaidx"
    if pkg in sys.modules:
        return
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    m = types.ModuleType(pkg)
    m.__path__ = [str(ROOT)]
    sys.modules[pkg] = m
    lib = types.ModuleType(f"{pkg}.libraries")
    lib.__path__ = [str(ROOT / "libraries")]
    sys.modules[f"{pkg}.libraries"] = lib


_bootstrap()


def test_delayed_mkdir_and_share_snapshot() -> None:
    from nonebot_plugin_maimaidx.libraries import maimaidx_data_share as share_mod
    from nonebot_plugin_maimaidx.libraries import maimaidx_data_storage as ds_mod
    from nonebot_plugin_maimaidx.libraries.maimaidx_data_share import DataShareManager
    from nonebot_plugin_maimaidx.libraries.maimaidx_data_storage import DataStorageManager
    from nonebot_plugin_maimaidx.libraries.maimaidx_share_snapshot import (
        maybe_save_share_snapshot,
        playinfo_to_score_records,
    )

    class _R:
        def __init__(self, i: int):
            self.song_id = i
            self.title = f"t{i}"
            self.level = "14"
            self.level_index = 3
            self.ds = 14.0
            self.achievements = 100.5
            self.rate = "sssp"
            self.ra = 300
            self.fc = None
            self.fs = None
            self.dxScore = 0

    class _U:
        nickname = "tester"
        username = "tester"
        rating = 15000

    with tempfile.TemporaryDirectory() as tmp:
        scores = Path(tmp) / "user_scores"
        share_cfg = Path(tmp) / "share.json"
        scores.mkdir()
        original = ds_mod.DATA_DIR
        original_share = share_mod.data_share
        ds_mod.DATA_DIR = scores
        share_mod.data_share = DataShareManager(share_cfg)
        try:
            storage = DataStorageManager()
            # 只读不应创建目录
            _ = storage.list_snapshots(12345, limit=1)
            assert not (scores / "12345").exists()

            assert share_mod.data_share.is_sharing_enabled(12345)

            records = [_R(i) for i in range(1, 40)]
            assert len(playinfo_to_score_records(records)) == 39
            ok = maybe_save_share_snapshot(12345, _U(), records, source="test")
            assert ok is True
            assert (scores / "12345" / "index.json").exists()

            # opt-out 后不再写
            share_mod.data_share.opt_out(12345)
            ok2 = maybe_save_share_snapshot(12345, _U(), records, source="test2")
            assert ok2 is False
        finally:
            ds_mod.DATA_DIR = original
            share_mod.data_share = original_share


if __name__ == "__main__":
    test_delayed_mkdir_and_share_snapshot()
    print("ok")
