"""Static regression checks for the gated waterfish OAuth write path."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
api = (ROOT / 'libraries' / 'maimaidx_api_data.py').read_text(encoding='utf-8')
oauth = (ROOT / 'libraries' / 'maimaidx_divingfish_oauth.py').read_text(encoding='utf-8')
converter = (ROOT / 'libraries' / 'maimaidx_lxns_client.py').read_text(encoding='utf-8')
account = (ROOT / 'command' / 'mai_account.py').read_text(encoding='utf-8')
config = (ROOT / 'config.py').read_text(encoding='utf-8')
break_config = (ROOT / 'libraries' / 'maimaidx_break.py').read_text(encoding='utf-8')

assert 'divingfish_oauth_enabled: bool = False' in config
assert "'divingfish_oauth_enabled': '0'" in break_config
assert "_DB_SWITCH_KEY = 'divingfish_oauth_enabled'" in oauth
assert "SCOPE = 'prober.records.read prober.records.write'" in oauth
assert "'/player/update_records'" in api
assert 'async def update_records_oauth(' in api
assert "if not oauth_enabled():" in api
assert 'max_retries=0' in api
assert 'convert_sega_music_scores_to_divingfish' in converter
assert 'convert_pc_records_to_divingfish_scores' in converter
assert 'maiApi.update_records_oauth(qqid, fish_records)' in account
assert 'fish_oauth = bool(' in account
assert 'not binding.fish_token' in account

print('divingfish oauth write tests: ok')
