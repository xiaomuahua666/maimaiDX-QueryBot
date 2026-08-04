"""qbind 必须以论坛 OAuth 绑定查分 QQ。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
qq_bind = (ROOT / "command" / "mai_qq_bind.py").read_text(encoding="utf-8")
forum_auth = (ROOT / "libraries" / "maimaidx_forum_auth.py").read_text(encoding="utf-8")
qq_db = (ROOT / "libraries" / "maimaidx_qq_bind.py").read_text(encoding="utf-8")
error_source = (ROOT / "libraries" / "maimaidx_error.py").read_text(encoding="utf-8")
forum_cmd = (ROOT / "command" / "mai_forum_bind.py").read_text(encoding="utf-8")

assert "begin_forum_login(pid)" in qq_bind
assert "begin_forum_login(pid, claimed_qq=claimed)" in qq_bind
assert "complete_forum_login" in qq_bind
assert "论坛 OAuth" in qq_bind
assert "qq_bind_db.bind(pid, qq)" not in qq_bind  # no direct trust of typed QQ
assert "'论坛绑定'" in qq_bind

assert "claimed_qq" in forum_auth
assert "OAuth 校验失败" in forum_auth
assert "claimed_qq=claimed_qq" in forum_auth

assert "claimed_qq" in qq_db
assert "ALTER TABLE forum_oauth_pending ADD COLUMN claimed_qq" in qq_db

assert "qbind（论坛 OAuth" in error_source
assert "on_command(\n    '论坛绑定'" not in forum_cmd
assert "强制绑定QQ" in forum_cmd

print("qbind oauth tests: ok")
