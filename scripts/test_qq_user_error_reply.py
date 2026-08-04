"""官方 QQ 未绑定时必须回复 OAuth 提示，不能静默失败。"""

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
assert "ensure_context" in platform_source
assert "raise FinishedException" in platform_source
assert "require_account_qqid" in platform_source
assert "payload_to_event" in platform_source
assert "dependencies.check_field_type = _check_field_type" in platform_source
assert "Dependent._solve_field" in platform_source

assert "qbind（论坛 OAuth" in error_source
assert "数字@qq.com" in error_source or "你的QQ号@qq.com" in error_source

qq_bind_source = (ROOT / "command" / "mai_qq_bind.py").read_text(encoding="utf-8")
assert "_maimaidx_announcement_exempt" in qq_bind_source
assert "_maimaidx_debt_exempt" in qq_bind_source
assert "'/qbind'" not in qq_bind_source
assert "begin_forum_login" in qq_bind_source

assert "_account_qqid" in break_source
assert "require_account_qqid" in break_source
assert "qqid = _account_qqid(event)" in break_source

print("qq user error reply tests: ok")
