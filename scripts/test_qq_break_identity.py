#!/usr/bin/env python3
"""BREAK transfers/admin commands accept official-QQ identities."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

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

import nonebot_plugin_maimaidx  # noqa: F401, E402
from nonebot_plugin_maimaidx.command import mai_break  # noqa: E402
from nonebot_plugin_maimaidx.libraries import maimaidx_bot_admin  # noqa: E402
from nonebot_plugin_maimaidx.libraries.maimaidx_qq_bind import QqBindDatabase  # noqa: E402


class OfficialEvent:
    user_id = "admin-openid"

    def get_user_id(self):
        return self.user_id


original_parse = mai_break.parse_at_target_id
original_normalize = mai_break.normalize_billing_qqid
original_admin_ids = maimaidx_bot_admin.get_plugin_admin_ids
original_legacy = QqBindDatabase.get_legacy_qq
try:
    mai_break.parse_at_target_id = lambda _event: "unbound-target-openid"
    mai_break.normalize_billing_qqid = lambda value: (
        246801357 if str(value) == "unbound-target-openid" else None
    )
    assert mai_break.get_at_qq(OfficialEvent()) == 246801357

    maimaidx_bot_admin.get_plugin_admin_ids = lambda: {"987654321"}
    QqBindDatabase.get_legacy_qq = lambda _self, value: (
        987654321 if str(value) == "admin-openid" else None
    )
    assert maimaidx_bot_admin.is_plugin_admin("admin-openid")
finally:
    mai_break.parse_at_target_id = original_parse
    mai_break.normalize_billing_qqid = original_normalize
    maimaidx_bot_admin.get_plugin_admin_ids = original_admin_ids
    QqBindDatabase.get_legacy_qq = original_legacy

source = (ROOT / "command" / "mai_break.py").read_text(encoding="utf-8")
assert "permission=PLUGIN_ADMIN_ONLY" in source
print("qq BREAK identity tests: ok")
