"""水鱼 OAuth 路由、短期缓存与 AWMCNET 兼容回归测试。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = 'divingfish_oauth_test_package'

root_package = types.ModuleType(PACKAGE)
root_package.__path__ = [str(ROOT)]
libraries_package = types.ModuleType(f'{PACKAGE}.libraries')
libraries_package.__path__ = [str(ROOT / 'libraries')]
config_module = types.ModuleType(f'{PACKAGE}.config')
config_module.maiconfig = SimpleNamespace(
    divingfish_oauth_enabled=False,
    divingfish_client_id='client-test',
    divingfish_client_secret='secret-test',
    divingfish_auth_url='https://auth.example.test',
)
config_module.log = SimpleNamespace(warning=lambda *_args, **_kwargs: None)

error_module = types.ModuleType(f'{PACKAGE}.libraries.maimaidx_error')


class DivingFishNotAuthorizedError(Exception):
    pass


class DivingFishOAuthError(Exception):
    pass


error_module.DivingFishNotAuthorizedError = DivingFishNotAuthorizedError
error_module.DivingFishOAuthError = DivingFishOAuthError

sys.modules[PACKAGE] = root_package
sys.modules[f'{PACKAGE}.libraries'] = libraries_package
sys.modules[f'{PACKAGE}.config'] = config_module
sys.modules[f'{PACKAGE}.libraries.maimaidx_error'] = error_module

spec = importlib.util.spec_from_file_location(
    f'{PACKAGE}.libraries.maimaidx_divingfish_oauth',
    ROOT / 'libraries' / 'maimaidx_divingfish_oauth.py',
)
assert spec and spec.loader
oauth = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = oauth
spec.loader.exec_module(oauth)


async def run_oauth_checks() -> None:
    qqid = 123456789
    oauth._load_db_switch = lambda: None
    oauth.reload_oauth_config()
    assert not oauth.oauth_switch_enabled()
    assert not oauth.oauth_enabled()
    config_module.maiconfig.divingfish_oauth_enabled = True
    assert oauth.oauth_enabled()
    oauth._load_db_switch = lambda: False
    oauth.reload_oauth_config()
    assert not oauth.oauth_enabled()
    oauth._load_db_switch = lambda: None
    oauth.reload_oauth_config()
    assert oauth.oauth_enabled()
    assert oauth.subject_ref(qqid) == hashlib.sha256(
        b'client-test:123456789'
    ).hexdigest()
    assert oauth.binding_label(qqid) == 'QQ 12*****89'
    assert oauth.revoke_url() == 'https://auth.example.test/apps'

    calls = []

    async def fake_post(path, data):
        calls.append((path, data))
        if path == oauth.DEVICE_AUTHORIZATION_PATH:
            return {
                'verification_uri_complete': 'https://auth.example.test/device?code=masked',
                'expires_in': 600,
                'interval': 5,
            }
        return {
            'access_token': f'access-{len(calls)}',
            'expires_in': 300,
        }

    oauth._post = fake_post
    authorization = await oauth.create_device_authorization(qqid)
    assert authorization.expires_in == 600
    assert calls[0][1]['subject_ref'] == oauth.subject_ref(qqid)
    assert '123456789' not in calls[0][1]['subject_ref']

    token_1 = await oauth.get_access_token(qqid)
    token_2 = await oauth.get_access_token(qqid)
    assert token_1 == token_2
    assert len([item for item in calls if item[0] == oauth.TOKEN_PATH]) == 1

    oauth.invalidate_access_token(qqid, 'a-different-token')
    assert await oauth.get_access_token(qqid) == token_1
    oauth.invalidate_access_token(qqid, token_1)
    assert await oauth.get_access_token(qqid) != token_1


asyncio.run(run_oauth_checks())

config_source = (ROOT / 'config.py').read_text(encoding='utf-8')
api_source = (ROOT / 'libraries' / 'maimaidx_api_data.py').read_text(encoding='utf-8')
command_source = (ROOT / 'command' / 'mai_divingfish.py').read_text(encoding='utf-8')
account_source = (ROOT / 'command' / 'mai_account.py').read_text(encoding='utf-8')
break_source = (ROOT / 'libraries' / 'maimaidx_break.py').read_text(encoding='utf-8')
datasource_source = (ROOT / 'libraries' / 'maimaidx_datasource.py').read_text(
    encoding='utf-8'
)

assert 'divingfish_client_id' in config_source
assert 'divingfish_client_secret' in config_source
assert 'divingfish_oauth_enabled' in config_source
assert "'/player/records'" in api_source
assert "'/player/record'" in api_source
assert "'/player/plate'" in api_source
assert "headers={'Authorization': f'Bearer {access_token}'}" in api_source
assert "aliases={'绑定水鱼', '绑定df', '水鱼授权'}" in command_source
assert 'if oauth_enabled():' in command_source
assert '绑定水鱼上传' in account_source
assert 'maibindfish' in account_source
assert 'if not divingfish_oauth_enabled():' in account_source
assert "'divingfish_oauth_enabled': '0'" in break_source

# AWMCNET 缺数据或强制刷新时仍会探测 divingfish；OAuth 返回的数据沿用同一同步器。
assert "force_source='divingfish'" in datasource_source
assert "await sync_awmcnet(qqid, userinfo, records, source='auto-migrate')" in datasource_source
assert 'if qqid and not username and divingfish_oauth_enabled():' in datasource_source
assert 'return _divingfish_dev_to_userinfo(dev)' in datasource_source

print('divingfish oauth tests: ok')
