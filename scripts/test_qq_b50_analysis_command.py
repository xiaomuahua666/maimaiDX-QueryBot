"""Official QQ B50 analysis must accept openids and acknowledge long work."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
source = (ROOT / "command" / "mai_b50_analysis.py").read_text(encoding="utf-8")

assert "int(event.get_user_id())" not in source
assert "int(platform_user_id(event))" not in source
assert "billing_qq = billing_user_id(event)" in source
assert "legacy_qq = resolve_score_qqid(event)" in source
assert "if use_qq_mode(event) or not bool(" in source
assert "plugin_send(" in source
assert "mention_sender=use_qq_mode(event)" in source
assert "await plugin_finish(" in source
assert "正在处理 B50 锐评，请稍候" in source
assert source.index("plugin_send(") < source.index("react_processing(")
assert "b50_reaction_timeout_seconds" in source
assert "b50_fetch_timeout_seconds" in source
assert "b50_llm_timeout_seconds" in source
assert "b50_llm_max_tokens" in (ROOT / "libraries" / "b50_analysis" / "llm.py").read_text(encoding="utf-8")
assert "b50_send_timeout_seconds" in source
assert "asyncio.wait_for(" in source
assert "_run_timed_stage(" in source

print("qq b50 analysis command tests: ok")
