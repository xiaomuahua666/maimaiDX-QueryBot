"""主群↔猜歌群同步：源码挂载检查 + 手动迁移确认状态机。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_sync_only() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    pkg = "nonebot_plugin_maimaidx"
    if pkg not in sys.modules:
        m = types.ModuleType(pkg)
        m.__path__ = [str(ROOT)]
        sys.modules[pkg] = m
    lib_name = f"{pkg}.libraries"
    if lib_name not in sys.modules:
        lib = types.ModuleType(lib_name)
        lib.__path__ = [str(ROOT / "libraries")]
        sys.modules[lib_name] = lib

    cfg = types.ModuleType(f"{pkg}.config")
    cfg.guess_sync_prefs_file = ROOT / "static" / "group_guess_sync_prefs.json"
    sys.modules[f"{pkg}.config"] = cfg

    tool = types.ModuleType(f"{pkg}.libraries.tool")

    async def writefile(file: Path, data):
        Path(file).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    tool.writefile = writefile
    sys.modules[f"{pkg}.libraries.tool"] = tool

    plat = types.ModuleType(f"{pkg}.libraries.maimaidx_platform")
    plat.GroupId = object
    plat.UserId = object
    plat.resolve_group_legacy_id = lambda _gid: None
    sys.modules[f"{pkg}.libraries.maimaidx_platform"] = plat


async def _run_state_machine() -> None:
    _bootstrap_sync_only()
    from nonebot_plugin_maimaidx.libraries.maimaidx_guess_sync import (
        CONFLICT_PROMPT,
        MAIN_GROUP_REDIRECT,
        MAIN_GUESS_GROUP_ID,
        PLAY_GUESS_GROUP_ID,
        GuessSyncManager,
    )

    with tempfile.TemporaryDirectory() as tmp:
        prefs = Path(tmp) / "prefs.json"
        sync = GuessSyncManager(prefs)

        action, msg = await sync.prepare_group_entry(MAIN_GUESS_GROUP_ID, "1")
        assert action == "redirect" and msg == MAIN_GROUP_REDIRECT
        action, msg = await sync.prepare_group_entry(PLAY_GUESS_GROUP_ID, "1")
        assert action == "ok" and msg is None

        # 手动迁移：冲突确认
        sync.set_pending(PLAY_GUESS_GROUP_ID, "42", "choose")
        handled, reply = await sync.handle_reply(PLAY_GUESS_GROUP_ID, "42", "是")
        assert handled and "二次确认" in reply

        async def _ow(_uid):
            return True

        sync.apply_overwrite_from_main = _ow  # type: ignore[method-assign]
        handled, reply = await sync.handle_reply(PLAY_GUESS_GROUP_ID, "42", "确认")
        assert handled and "覆盖" in reply

        # 空本群导入确认
        sync.set_pending(PLAY_GUESS_GROUP_ID, "42", "confirm_import")
        handled, reply = await sync.handle_reply(PLAY_GUESS_GROUP_ID, "42", "确认")
        assert handled and ("导入" in reply or "覆盖" in reply)

        assert CONFLICT_PROMPT.startswith("感谢游玩AWMC猜歌")


def _run_source_checks() -> None:
    guess_src = (ROOT / "command" / "mai_guess.py").read_text(encoding="utf-8")
    assert "guess_migrate_data" in guess_src
    assert "迁移数据" in guess_src
    assert "start_manual_migrate" in guess_src
    assert "mirror_peer_if_needed" not in (
        ROOT / "libraries" / "maimaidx_guess_score.py"
    ).read_text(encoding="utf-8")
    letter_src = (ROOT / "command" / "mai_letter.py").read_text(encoding="utf-8")
    assert "start_manual_migrate" not in letter_src
    assert "prepare_group_entry" in letter_src
    sync_src = (ROOT / "libraries" / "maimaidx_guess_sync.py").read_text(encoding="utf-8")
    assert "start_manual_migrate" in sync_src
    assert "async def mirror_peer_if_needed" not in sync_src


if __name__ == "__main__":
    _run_source_checks()
    asyncio.run(_run_state_machine())
    print("guess sync tests: ok")
