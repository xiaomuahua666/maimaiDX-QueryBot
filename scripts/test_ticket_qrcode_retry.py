"""发票二维码 180 秒续发状态回归测试（无需启动 NoneBot）。"""

import ast
import time
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_PATH = ROOT / "command" / "mai_account.py"
PLAYCOUNT_PATH = ROOT / "command" / "mai_playcount.py"


tree = ast.parse(ACCOUNT_PATH.read_text(encoding="utf-8"))
names = {
    "remember_pending_ticket_retry",
    "take_pending_ticket_retry",
    "clear_pending_ticket_retry",
}
selected = [
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name in names
]
assert {node.name for node in selected} == names
namespace = {
    "Optional": Optional,
    "time": time,
    "_TICKET_QRCODE_RETRY_SECONDS": 180,
    "_pending_ticket_retries": {},
}
exec(
    compile(ast.Module(body=selected, type_ignores=[]), str(ACCOUNT_PATH), "exec"),
    namespace,
)

remember = namespace["remember_pending_ticket_retry"]
take = namespace["take_pending_ticket_retry"]
clear = namespace["clear_pending_ticket_retry"]

deadline = remember("10001", 3, now=1000.0)
assert deadline == 1180.0
assert take("10001", now=1179.9) == (3, 1180.0)
assert take("10001", now=1179.9) is None

remember("10001", 5, now=2000.0)
assert take("10001", now=2180.0) is None

remember("10001", 2, expires_at=3050.0, now=3000.0)
clear("10001")
assert take("10001", now=3001.0) is None

account_source = ACCOUNT_PATH.read_text(encoding="utf-8")
assert "请在 180 秒内重新发送最新 SGWCMAID" in account_source
assert "continue_ticket_with_qrcode" in account_source
assert "倍票已受理" not in account_source
assert "发票不可逆：票券一经发放必须上机使用，不可以屯票" in account_source
execute_start = account_source.index("async def _execute_ticket_now(")
execute_end = account_source.index("\n\nasync def _execute_ticket(", execute_start)
execute_source = account_source[execute_start:execute_end]
assert execute_source.index("await notify(_TICKET_IRREVERSIBLE_NOTICE)") < execute_source.index(
    "await _read_verified_preview("
)

playcount_source = PLAYCOUNT_PATH.read_text(encoding="utf-8")
pending_pos = playcount_source.index("pending_ticket = take_pending_ticket_retry")
ticket_only_pos = playcount_source.index("if pending_ticket is not None:", pending_pos)
dedupe_guard = (
    "if pending_ticket is None and pending_account is None "
    "and _qrcode_dedupe_hit"
)
dedupe_pos = playcount_source.index(dedupe_guard, pending_pos)
auto_upload_pos = playcount_source.index("previous = account_db.get", pending_pos)
assert pending_pos < ticket_only_pos < dedupe_pos < auto_upload_pos
ticket_branch = playcount_source[ticket_only_pos:auto_upload_pos]
assert "continue_ticket_with_qrcode" in ticket_branch
assert "_sync_sdgb_qrcode" not in ticket_branch
assert "_upload" not in ticket_branch
assert "continue_ticket_with_qrcode" in playcount_source[auto_upload_pos:]

# A refreshed QR for a pending account query must bypass the normal sync
# workflow and continue the requested operation directly.
pending_account_direct_pos = playcount_source.index(
    "if pending_account is not None:", pending_pos
)
assert pending_account_direct_pos < dedupe_pos
pending_account_branch = playcount_source[pending_account_direct_pos:dedupe_pos]
assert "continue_pending_account_retry" in pending_account_branch
assert "_sync_sdgb_qrcode" not in pending_account_branch
assert "_upload" not in pending_account_branch

# Chime 3001 (二维码已过期) must be treated as an expired SGID so pending
# ticket/account retries prompt for a fresh QR instead of failing forever.
assert '"3001"' in account_source
assert "3001" in account_source
assert '"二维码已过期"' in account_source

print("ticket qrcode retry tests: ok")
