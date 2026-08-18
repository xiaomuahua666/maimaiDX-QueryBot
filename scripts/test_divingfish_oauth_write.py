"""Static regression checks for the gated waterfish OAuth write path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
api = (ROOT / 'libraries' / 'maimaidx_api_data.py').read_text(encoding='utf-8')
oauth = (ROOT / 'libraries' / 'maimaidx_divingfish_oauth.py').read_text(encoding='utf-8')
converter = (ROOT / 'libraries' / 'maimaidx_lxns_client.py').read_text(encoding='utf-8')
account = (ROOT / 'command' / 'mai_account.py').read_text(encoding='utf-8')
datasource = (ROOT / 'libraries' / 'maimaidx_datasource.py').read_text(encoding='utf-8')
config = (ROOT / 'config.py').read_text(encoding='utf-8')
break_config = (ROOT / 'libraries' / 'maimaidx_break.py').read_text(encoding='utf-8')
admin_web = (ROOT / 'libraries' / 'maimaidx_admin_web.py').read_text(encoding='utf-8')

assert 'divingfish_oauth_enabled: bool = False' in config
assert "'divingfish_oauth_enabled': '0'" in break_config
assert "_DB_SWITCH_KEY = 'divingfish_oauth_enabled'" in oauth
assert "SCOPE = 'profile prober.profile.read prober.records.read prober.records.write'" in oauth
assert "'/player/update_records'" in api
assert 'async def update_records_oauth(' in api
assert "if not oauth_enabled():" in api
assert 'max_retries=0' in api
assert '200 <= res.status_code < 300' in api
assert 'range(0, len(records), 1000)' in api
assert 'convert_sega_music_scores_to_divingfish' in converter
assert 'convert_pc_records_to_divingfish_scores' in converter
assert 'maiApi.update_records_oauth(qqid, fish_records)' in account
assert 'await get_divingfish_access_token(qqid)' in account
assert 'async def _resolve_upload_channels(' in account
assert 'fish=channels.fish, lxns=channels.lxns' in account
assert '旧 Token 已停用，请重新发送「绑定水鱼」完成 OAuth' in account
assert '旧 Token 不会回退使用' in account
assert "source == 'divingfish' and qqid and not username and divingfish_oauth_enabled()" in datasource
assert datasource.count('await get_divingfish_access_token(qqid)') >= 2
assert 'reload_oauth_config()' in admin_web
assert 'has_fish_oauth' in account
assert "if raw_id > 100000:\n        return None" in converter

print('divingfish oauth write tests: ok')
