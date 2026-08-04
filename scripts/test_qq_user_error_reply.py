"""官方 QQ 未 qbind 时必须回复提示，不能静默失败。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
platform_source = (ROOT / "libraries" / "maimaidx_platform.py").read_text(
    encoding="utf-8"
)
break_source = (ROOT / "command" / "mai_break.py").read_text(encoding="utf-8")
error_source = (ROOT / "libraries" / "maimaidx_error.py").read_text(encoding="utf-8")

assert "_maimaidx_user_error_reply" in platform_source
assert "QBindRequiredError" in platform_source
assert "format_command_error" in platform_source
assert "Matcher.simple_run" in platform_source
assert "require_account_qqid" in platform_source
assert "payload_to_event" in platform_source

assert "_account_qqid" in break_source
assert "require_account_qqid" in break_source
assert "qqid = _account_qqid(event)" in break_source

assert "请发送：qbind 你的QQ号" in error_source

print("qq user error reply tests: ok")
