"""公告数据库与命令门禁回归测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from libraries.maimaidx_announcement import (
    AnnouncementDatabase,
    format_announcement,
)


with TemporaryDirectory() as tmp:
    db = AnnouncementDatabase(Path(tmp) / "announcement.db")

    old_required = db.create("旧必读公告", required=True)
    assert old_required.is_current
    assert db.unseen_current("user") == old_required

    current_optional = db.create("最新普通公告")
    assert not db.get(old_required.id).is_current
    assert db.current() == current_optional
    # 只看最新公告，旧必读公告不能再拦住长期未使用 Bot 的用户。
    assert db.unseen_current("user") == current_optional
    assert db.claim_optional_current("user") == current_optional
    assert db.claim_optional_current("user") is None
    assert db.unseen_current("user") is None

    # 编辑历史公告不会重新激活它。
    edited_old = db.update(old_required.id, content="旧公告修订")
    assert edited_old is not None and not edited_old.is_current
    assert db.current().id == current_optional.id

    # 编辑当前公告生成新版本，已读用户会再看到一次。
    edited_current = db.update(current_optional.id, content="最新公告修订")
    assert edited_current is not None
    assert edited_current.revision == 2
    assert db.unseen_current("user") == edited_current

    # 改为必读后必须确认当前版本，确认后不再重复。
    required_current = db.update(current_optional.id, required=True)
    assert required_current is not None and required_current.required
    assert required_current.revision == 3
    assert db.unseen_current("user") == required_current
    assert db.mark_seen("user", required_current.id, required_current.revision)
    assert db.unseen_current("user") is None
    assert not db.mark_seen("user", required_current.id, required_current.revision)

    public_text = format_announcement(required_current, show_id=False)
    admin_text = format_announcement(required_current, show_id=True)
    assert f"#{required_current.id}" not in public_text
    assert f"#{required_current.id}" in admin_text
    assert "发布时间：" in public_text and "更新时间：" in public_text

    # 删除当前公告后没有生效公告，不回退启用历史必读公告。
    deleted = db.delete(required_current.id)
    assert deleted is not None and deleted.is_current
    assert db.current() is None
    assert db.unseen_current("new-user") is None
    assert db.recent(10)[0].id == old_required.id


command_source = Path("command/mai_announcement.py").read_text(encoding="utf-8")
init_source = Path("command/__init__.py").read_text(encoding="utf-8")
account_source = Path("command/mai_account.py").read_text(encoding="utf-8")
playcount_source = Path("command/mai_playcount.py").read_text(encoding="utf-8")
admin_runtime_source = Path("command/mai_admin_runtime.py").read_text(encoding="utf-8")
assert "announcement_db.unseen_current" in command_source
assert "announcement_db.claim_optional_current" in command_source
assert "await asyncio.sleep(1)" in command_source
assert "_pending_required" in command_source
assert "确认阅读公告" in command_source
assert "message_may_contain_qrcode" in command_source
assert "enforce_current_announcement" in command_source
assert 'state.pop("__maimaidx_serial_user_operation", None)' in command_source
assert "finish_account_operation(_user_key(event))" in command_source
assert "_maimaidx_passive_recorder" in command_source
assert "_maimaidx_deferred_audit" in command_source
assert "_maimaidx_debt_exempt" in command_source
assert account_source.count("enforce_current_announcement(bot, event)") == 2
assert "_maimaidx_announcement_exempt" in playcount_source
assert "enforce_current_announcement(bot, event)" in playcount_source
assert 'setattr(_message_recorder, "_maimaidx_passive_recorder", True)' in admin_runtime_source
assert init_source.index("mai_announcement") < init_source.index("mai_admin_runtime")

print("announcement tests: ok")
