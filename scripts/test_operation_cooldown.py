"""抽奖概率、用户操作互斥与机台冷却回归测试（无需启动 NoneBot）。"""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


break_tree = ast.parse(
    (ROOT / "libraries" / "maimaidx_break.py").read_text(encoding="utf-8")
)
constants = {}
for node in break_tree.body:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        continue
    target = node.targets[0]
    if isinstance(target, ast.Name) and target.id in {
        "LOTTERY_PRIZES",
        "LOTTERY_WEIGHTS",
    }:
        constants[target.id] = ast.literal_eval(node.value)

assert constants["LOTTERY_PRIZES"] == (0, 1, 2, 5, 10)
assert constants["LOTTERY_WEIGHTS"] == (35, 30, 20, 12, 3)
expected_return = sum(
    prize * weight
    for prize, weight in zip(
        constants["LOTTERY_PRIZES"], constants["LOTTERY_WEIGHTS"]
    )
) / sum(constants["LOTTERY_WEIGHTS"])
assert expected_return == 1.6
assert sum(constants["LOTTERY_WEIGHTS"][1:]) == 65

runtime_path = ROOT / "command" / "mai_admin_runtime.py"
runtime_source = runtime_path.read_text(encoding="utf-8")
runtime_tree = ast.parse(runtime_source)
selected = [
    node
    for node in runtime_tree.body
    if isinstance(node, ast.FunctionDef)
    and node.name in {"_serial_user_operation", "_release_user_operation"}
]
active_operations: set[str] = set()
namespace = {
    "Matcher": object,
    "T_State": dict,
    "finish_account_operation": active_operations.discard,
}
exec(
    compile(ast.Module(body=selected, type_ignores=[]), str(runtime_path), "exec"),
    namespace,
)


class SerialMatcher:
    _maimaidx_serial_user_operation = True


class NormalMatcher:
    pass


assert namespace["_serial_user_operation"](SerialMatcher())
assert not namespace["_serial_user_operation"](NormalMatcher())
active_operations.add("10001")
state = {"__maimaidx_serial_user_operation": "10001"}
namespace["_release_user_operation"](state)
assert "10001" not in active_operations
assert not state

playcount_source = (ROOT / "command" / "mai_playcount.py").read_text(encoding="utf-8")
assert "setattr(update_pc, '_maimaidx_serial_user_operation', True)" in playcount_source
assert "if not try_begin_account_operation(qqid):" in playcount_source
assert "finish_account_operation(qqid)" in playcount_source

account_source = (ROOT / "command" / "mai_account.py").read_text(encoding="utf-8")
assert "setattr(_serial_account_matcher, '_maimaidx_serial_user_operation', True)" in account_source
assert "操作已确认" not in runtime_source
assert "你已有一个操作正在进行" not in runtime_source
assert "操作已确认" not in account_source
assert "📤 已受理，正在上传到" in account_source
assert "def _upload_can_start_now(" in account_source
assert "if _upload_can_start_now(" in account_source
assert "async def _refresh_b50_cache_after_upload(" in account_source
assert "await get_user_records(" in account_source
assert "await get_user_b50(" in account_source
assert "force_source=source, force_refresh=True" in account_source
assert "_schedule_post_upload_maintenance(" in account_source
assert "fish=False," in account_source and "lxns=True," in account_source
assert "fish=fish," in account_source and "lxns=lxns," in account_source
assert 'else "upload_awmcnet"' in account_source
assert 'billing_service = "upload" if (fish or lxns) else "awmcnet_sync"' in account_source
assert 'cost = _service_cost(operation) if (fish or lxns) else 0' in account_source
assert "async def _post_upload_maintenance(" in account_source
assert "task.add_done_callback(_post_upload_tasks.discard)" in account_source
assert "二维码缓存已过期，请重新发送最新 SGWCMAID" in account_source
assert "maiu_cache_listener" not in playcount_source
assert "_MAIU_UPLOAD_GRACE_SECONDS" not in playcount_source

letter_source = (ROOT / "command" / "mai_letter.py").read_text(encoding="utf-8")
assert "正在结算本局贡献" not in letter_source
assert "settlement_lines.extend(feedback.goal_tips)" in letter_source
assert "settlement_lines.extend(feedback.record_tips)" in letter_source
assert 'msg = Message("\\n".join(settlement_lines) + "\\n")' in letter_source

assert "negative BREAK balance" in runtime_source
assert "已暂停其他功能" in runtime_source
assert "_DEBT_NOTICE_COOLDOWN_SECONDS = 300" in runtime_source
assert "_debt_notified.get(debt_key, 0)" in runtime_source
deferred_pos = runtime_source.index('"_maimaidx_deferred_audit"')
debt_pos = runtime_source.index("balance < 0")
assert deferred_pos < debt_pos
for exempt_path in (
    ROOT / "command" / "mai_break.py",
    ROOT / "command" / "mai_base.py",
    ROOT / "command" / "mai_agreement.py",
):
    assert "_maimaidx_debt_exempt" in exempt_path.read_text(encoding="utf-8")

sw_api_source = (ROOT / "libraries" / "maimaidx_sw_api.py").read_text(encoding="utf-8")
assert "awmc_api_success_cooldown_seconds" in sw_api_source
assert "await asyncio.sleep(cooldown)" in sw_api_source
response_pos = sw_api_source.index("data = res.json()")
sleep_pos = sw_api_source.index("await asyncio.sleep(cooldown)", response_pos)
return_pos = sw_api_source.index("return data", sleep_pos)
assert response_pos < sleep_pos < return_pos

print("operation cooldown tests: ok")
