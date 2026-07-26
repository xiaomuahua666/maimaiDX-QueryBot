"""数据集自动落盘：PC 兜底与 qqid 收集（无需 NoneBot）。"""

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


def test_collect_and_pc_archive() -> None:
    from nonebot_plugin_maimaidx.libraries import maimaidx_data_share as share_mod
    from nonebot_plugin_maimaidx.libraries import maimaidx_data_storage as ds_mod
    from nonebot_plugin_maimaidx.libraries import maimaidx_dataset_archive as arch
    from nonebot_plugin_maimaidx.libraries import maimaidx_playcount_db as pc_mod
    from nonebot_plugin_maimaidx.libraries.maimaidx_data_share import DataShareManager
    from nonebot_plugin_maimaidx.libraries.maimaidx_data_storage import DataStorageManager
    from nonebot_plugin_maimaidx.libraries.maimaidx_playcount_db import (
        PlayCountDatabase,
        PlayCountRecord,
    )

    assert arch.collect_archive_qqids("1", 1, None, "x", 2, 0) == [1, 2]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        scores = tmp_path / "user_scores"
        share_cfg = tmp_path / "share.json"
        pc_dir = tmp_path / "playcount"
        scores.mkdir()
        pc_dir.mkdir()

        old_data = ds_mod.DATA_DIR
        old_share = share_mod.data_share
        old_pc_dir = pc_mod.DB_DIR
        old_pc_file = pc_mod.DB_FILE
        old_pc_singleton = PlayCountDatabase._instance

        ds_mod.DATA_DIR = scores
        share_mod.data_share = DataShareManager(share_cfg)
        pc_mod.DB_DIR = pc_dir
        pc_mod.DB_FILE = pc_dir / "playcount.db"
        PlayCountDatabase._instance = None
        try:
            storage = DataStorageManager()
            assert share_mod.data_share.is_sharing_enabled(424242)

            now = 1.0
            records = [
                PlayCountRecord(
                    song_id=i,
                    title=f"t{i}",
                    level="14",
                    level_index=3,
                    play_count=1,
                    achievements=100.5,
                    rate="sssp",
                    dx_score=1000,
                    dx_rating=14.0,
                    fc="",
                    fs="",
                    updated_at=now,
                )
                for i in range(1, 40)
            ]
            pc_db = PlayCountDatabase()
            pc_db.save_play_count_records(424242, records)

            ok = arch.maybe_archive_from_playcount(424242, source="test_pc")
            assert ok is True
            assert (scores / "424242" / "index.json").exists()
            from datetime import date as _date

            snap = storage.load_daily_snapshot(424242, _date.today().isoformat())
            assert snap is not None
            assert snap.record_count >= 30
            assert snap.source == "test_pc"

            share_mod.data_share.opt_out(424242)
            assert arch.maybe_archive_from_playcount(424242, source="test_pc2") is False
        finally:
            ds_mod.DATA_DIR = old_data
            share_mod.data_share = old_share
            pc_mod.DB_DIR = old_pc_dir
            pc_mod.DB_FILE = old_pc_file
            PlayCountDatabase._instance = old_pc_singleton


if __name__ == "__main__":
    test_collect_and_pc_archive()
    # source checks
    account = (ROOT / "command" / "mai_account.py").read_text(encoding="utf-8")
    assert "archive_user_scores_for_dataset" in account
    assert "_archive_qqids_for_event" in account
    playcount = (ROOT / "command" / "mai_playcount.py").read_text(encoding="utf-8")
    assert "schedule_dataset_archive" in playcount
    print("dataset archive tests: ok")
