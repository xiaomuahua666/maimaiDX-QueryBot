# -*- coding: utf-8 -*-
"""验证艺术家别名双向解析 + understand 别名替换为官方名。"""
import sys
import types
import importlib
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# stub 掉 maimaidx_music，避免触发 NoneBot 配置加载
_model_mod = importlib.import_module('libraries.maimaidx_model')
_music_stub = types.ModuleType('libraries.maimaidx_music')
_music_stub.Music = _model_mod.Music


class _MaiStub:
    pass


class _GuessStub:
    pass


_music_stub.mai = _MaiStub()
_music_stub.guess = _GuessStub()
sys.modules['libraries.maimaidx_music'] = _music_stub

from libraries import maimaidx_guess_20q as g  # noqa: E402


def check(name, cond):
    print(('  [PASS] ' if cond else '  [FAIL] ') + name)
    check.failed += 0 if cond else 1


check.failed = 0

print('=== 艺术家别名双向解析 ===')

cases = [
    ('匹诺曹P', 'ピノキオピー'),
    ('PinocchioP', 'ピノキオピー'),
    ('匹诺曹', 'ピノキオピー'),
    ('sakuzyo', '削除'),
    ('山茶花', 'かめりあ'),
    ('Camellia', 'かめりあ'),
    ('米津玄師', 'ハチ'),       # dxdata 若署米津玄師，应归到 ハチ 组
    ('Hachi', 'ハチ'),
    ('nulut', 'ぬゆり'),
    ('Lanndo', 'ぬゆり'),
    ('春卷饭', 'はるまきごはん'),
    ('nbuna', 'n-buna'),
    ('Kessoku Band', '結束バンド'),
    ('纽带乐队', '結束バンド'),
    ('夜鹿', 'ヨルシカ'),
    ('金爆', 'ゴールデンボンバー'),
]
for inp, expected_official in cases:
    off, als = g._resolve_artist_group(inp)
    ok = off == expected_official
    check(f'{inp!r:20} -> 官方名 {off!r}（期望 {expected_official!r}）', ok)

# 未在册的名字原样返回
off, als = g._resolve_artist_group('某个不在册的人XYZ')
check('未在册名字原样返回', off == '某个不在册的人XYZ' and als == ())

print('\n=== _artist_with_aliases 格式化 ===')
s = g._artist_with_aliases('ピノキオピー')
check('ピノキオピー 格式化含官方名和别名', s.startswith('ピノキオピー') and '匹诺曹P' in s and '别名' in s)
s2 = g._artist_with_aliases('无别名的人XYZ')
check('无别名只返回原名', s2 == '无别名的人XYZ')

print('\n=== understand 别名替换为官方名 ===')

# 构造一个假 music：艺术家 ピノキオピー，谱师 Luxizhel
basic = MagicMock()
basic.artist = 'ピノキオピー'
music = MagicMock()
music.basic_info = basic
# _get_master_charters 会读 charts，直接 mock
g._get_master_charters = lambda m: ['Luxizhel']

# 玩家用别名提问，LLM 回显别名 -> 应替换为官方名
u = g._canonicalize_understand('判断艺术家是否为匹诺曹', '艺术家是匹诺曹吗', music)
check('匹诺曹 -> ピノキオピー', u == '判断艺术家是否为ピノキオピー')

u2 = g._canonicalize_understand('判断谱师是否为泸溪河', '谱师是泸溪河吗', music)
check('泸溪河 -> Luxizhel', u2 == '判断谱师是否为Luxizhel')

# 玩家没用别名时，understand 里出现别名不该被替换（因为问题里没出现）
u3 = g._canonicalize_understand('判断艺术家是否为匹诺曹', '艺术家是谁', music)
check('问题未用别名则不替换', u3 == '判断艺术家是否为匹诺曹')

# 长别名优先
basic2 = MagicMock(); basic2.artist = 'かめりあ'
music2 = MagicMock(); music2.basic_info = basic2
g._get_master_charters = lambda m: []
u4 = g._canonicalize_understand('判断是否为山茶花', '是山茶花吗', music2)
check('山茶花 -> かめりあ', u4 == '判断是否为かめりあ')

print(f'\n=== 结果: {check.failed} failed ===')
sys.exit(1 if check.failed else 0)
