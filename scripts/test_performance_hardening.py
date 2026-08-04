"""全局卡顿治理：SQLite 与上传后台维护回归检查。"""

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sqlite_path = ROOT / "libraries" / "maimaidx_sqlite.py"
spec = importlib.util.spec_from_file_location("maimaidx_sqlite_test", sqlite_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as td:
    conn = sqlite3.connect(Path(td) / "perf.db")
    module.configure_sqlite_connection(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()

for relative in (
    "maimaidx_admin_audit.py",
    "maimaidx_db.py",
    "maimaidx_qq_member_registry.py",
    "maimaidx_account_db.py",
    "maimaidx_processing_time.py",
    "maimaidx_playcount_db.py",
    "maimaidx_lxns_db.py",
    "maimaidx_player_cache.py",
    "maimaidx_qq_bind.py",
    "maimaidx_whitelist.py",
):
    source = (ROOT / "libraries" / relative).read_text(encoding="utf-8")
    assert "configure_sqlite_connection(self._conn)" in source, relative

runtime = (ROOT / "command" / "mai_admin_runtime.py").read_text(encoding="utf-8")
assert "_MESSAGE_STATS_FLUSH_SECONDS = 2.0" in runtime
assert "admin_audit.record_messages, rows" in runtime
assert 'name="maimaidx-message-stats-flush"' in runtime
assert "ref_id = await asyncio.to_thread(" in runtime
assert "admin_audit.start_trace," in runtime
assert "await asyncio.to_thread(admin_audit.finish_trace" in runtime

audit = (ROOT / "libraries" / "maimaidx_admin_audit.py").read_text(encoding="utf-8")
assert "def record_messages(" in audit
assert "self._conn.executemany(" in audit

qq_bind = (ROOT / "command" / "mai_qq_bind.py").read_text(encoding="utf-8")
assert "await asyncio.to_thread(record_from_event, event)" in qq_bind

account = (ROOT / "command" / "mai_account.py").read_text(encoding="utf-8")
assert account.count("_schedule_post_upload_maintenance(") >= 3  # 2 call sites + def
assert "archive_user_scores_for_dataset" in account
assert "_archive_qqids_for_event" in account
assert "async def _post_upload_maintenance(" in account
assert "asyncio.create_task(" in account
assert "task.add_done_callback(_post_upload_tasks.discard)" in account

break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
ensure_start = break_source.index("    def _ensure_user(")
ensure_end = break_source.index("\n    def _today(", ensure_start)
ensure_source = break_source[ensure_start:ensure_end]
assert "SELECT 1 FROM break_users" in ensure_source
assert "if exists:" in ensure_source

print("performance hardening tests: ok")
