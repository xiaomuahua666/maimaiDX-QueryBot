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
):
    source = (ROOT / "libraries" / relative).read_text(encoding="utf-8")
    assert "configure_sqlite_connection(self._conn)" in source, relative

mysql_db = (ROOT / "libraries" / "maimaidx_db.py").read_text(encoding="utf-8")
assert "self._thread_state = threading.local()" in mysql_db
assert "innodb_lock_wait_timeout" in mysql_db

break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
assert "def _db_lock" in break_source
assert "if self._conn is not None and self._conn._backend == 'mysql':" in break_source
assert "TRANSACTION ISOLATION LEVEL READ COMMITTED" in mysql_db

# No asynchronous handler may call the remote BREAK/Card MySQL layer directly.
# A slow network read here pauses NoneBot's shared message event loop.
import ast

for folder in (ROOT / "command", ROOT / "libraries"):
    for path in folder.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for async_fn in (
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        ):
            for call in ast.walk(async_fn):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in {"break_db", "card_manager"}
                ):
                    continue
                current = call
                isolated = False
                while current is not async_fn and current in parents:
                    current = parents[current]
                    if isinstance(
                        current,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                    ) and current is not async_fn:
                        isolated = True
                        break
                    if (
                        isinstance(current, ast.Call)
                        and isinstance(current.func, ast.Attribute)
                        and isinstance(current.func.value, ast.Name)
                        and current.func.value.id == "asyncio"
                        and current.func.attr == "to_thread"
                    ):
                        isolated = True
                        break
                assert isolated, f"blocking MySQL call in async path: {path}:{call.lineno}"

runtime = (ROOT / "command" / "mai_admin_runtime.py").read_text(encoding="utf-8")
assert "_MESSAGE_STATS_FLUSH_SECONDS = 2.0" in runtime
assert "admin_audit.record_messages, rows" in runtime
assert 'name="maimaidx-message-stats-flush"' in runtime
assert "ref_id = await asyncio.to_thread(" in runtime
assert "admin_audit.start_trace," in runtime
assert "await asyncio.to_thread(admin_audit.finish_trace" in runtime
assert "await asyncio.to_thread(_break_balance, payer)" in runtime
assert "_apply_busy_surcharge, payer, surcharge, meta" in runtime

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

timing = (ROOT / "libraries" / "maimaidx_timing.py").read_text(encoding="utf-8")
assert "await asyncio.to_thread(ensure_query_affordable" in timing
assert "await asyncio.to_thread(ensure_image_render_affordable" in timing
assert "await asyncio.to_thread(settle_image_render" in timing

break_source = (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
assert "await asyncio.to_thread(break_db.get_balance, payer)" in break_source

ensure_start = break_source.index("    def _ensure_user(")
ensure_end = break_source.index("\n    def _today(", ensure_start)
ensure_source = break_source[ensure_start:ensure_end]
assert "SELECT 1 FROM break_users" in ensure_source
assert "if exists:" in ensure_source

table = (ROOT / "command" / "mai_table.py").read_text(encoding="utf-8")
assert "await finish_timed_sync(" in table
assert "run_timed_call(draw_rating" not in table

search = (ROOT / "command" / "mai_search.py").read_text(encoding="utf-8")
assert "await asyncio.to_thread(draw_multiver_chart" in search

scheduler = (ROOT / "libraries" / "maimaidx_data_scheduler.py").read_text(
    encoding="utf-8"
)
assert "success, snapshot = await asyncio.to_thread(_build_and_save_snapshot)" in scheduler
assert "enabled_users, share_from_cache = await asyncio.to_thread(" in scheduler
assert scheduler.count("users_to_store = await asyncio.to_thread(_missing_users)") == 2

break_command = (ROOT / "command" / "mai_break.py").read_text(encoding="utf-8")
assert "await asyncio.to_thread(break_db.expire_red_packets)" in break_command

print("performance hardening tests: ok")
