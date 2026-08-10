"""20问：判定依据(reason) + 并发锁 + 定数档位语义 的回归测试。

版本范围推理（「在X代以前/以后」）已移交 LLM，规则层不再处理；
本文件聚焦：reason 字段不泄露真值、并发锁、定数档位语义。
复用 test_guess_20q_prefix 的 stub 机制，不依赖 NoneBot/完整曲库。
"""
import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402
model_mod = importlib.import_module('libraries.maimaidx_model')
music_stub = types.ModuleType('libraries.maimaidx_music')
music_stub.Music = model_mod.Music


class _AliasList(list):
    def by_alias(self, music_alias):
        return []


class _MaiStub:
    total_alias_list = _AliasList()


music_stub.mai = _MaiStub()
music_stub.guess = types.SimpleNamespace()
sys.modules['libraries.maimaidx_music'] = music_stub

from libraries.maimaidx_guess_20q import (  # noqa: E402
    Guess20QData,
    classify_question, _YES, _NO,
    twentyq_guess,
)
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402
from collections import namedtuple  # noqa: E402


def _make_music(version='maimai でらっくす buddies'):
    notes = namedtuple('Notes', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1)
    bi = BasicInfo.model_validate({
        'title': 'T', 'artist': 'DECO*27', 'genre': 'niconico & VOCALOID',
        'bpm': 180, 'release_date': '', 'from': version, 'is_new': False,
        'version': version,
    })
    return Music(
        id='1', title='T', type='SD', ds=[10.0, 12.0, 13.5, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=notes, charter='サファ太') for _ in range(4)],
        basic_info=bi,
    )


m = _make_music()  # 双代 buddies

# ── classify_question 返回 reason，且不泄露真实值 ──
# 注：分类/谱师/版本已纳入规则匹配；艺术家等需语义理解的维度仍移交 LLM。
ans, consumed, reason = classify_question(m, '紫谱定数是13吗')
assert consumed and ans == _NO and reason
assert '紫谱' in reason and '13' in reason
# 不得泄露真实定数 14.6
assert '14.6' not in reason

ans, consumed, reason = classify_question(m, 'BPM大于100吗')
assert consumed and ans == _YES and 'BPM' in reason and '100' in reason
assert '180' not in reason

# 分类题由规则层命中（stub genre=niconico & VOCALOID → 是术曲=是）
_ans_g, _c_g, _r_g = classify_question(m, '是术曲吗')
assert _c_g is True and _ans_g == _YES, f'术曲题应规则命中回答是: consumed={_c_g}, ans={_ans_g}'
assert 'niconico' not in _r_g and 'vocaloid' not in _r_g, f'分类reason不应泄露真值: {_r_g}'
# 艺术家仍移交 LLM；版本已纳入规则（stub version=buddies → 是双代=是）
assert classify_question(m, '艺术家是deco27吗')[1] is False, '艺术家题移交 LLM'
# 版本顺序/前后题仍走 LLM
assert classify_question(m, '在雪代及以后吗')[1] is False, '版本顺序题移交 LLM'

# ── 并发锁：上一条没处理完时，第二条返回 busy ──
# 用 monkeypatch 让 _check_guess 挂起，模拟正在判定
import libraries.maimaidx_guess_20q as mod  # noqa: E402

orig = mod._check_guess
gate = asyncio.Event()


async def _slow_check(*a, **k):
    await gate.wait()
    return False

mod._check_guess = _slow_check
try:
    import time as _t
    data = Guess20QData(
        music=m, answers=['1'], max_questions=20, duration=600,
        started_at=_t.time(),
    )
    twentyq_guess.groups[999] = data

    async def driver():
        r1 = asyncio.create_task(
            twentyq_guess.process_message(999, 'u', 'n', '我猜 某曲')
        )
        await asyncio.sleep(0.05)  # 确保 r1 已拿到锁
        r2 = await twentyq_guess.process_message(999, 'u2', 'n2', '我问 是术曲吗')
        assert r2.get('kind') == 'busy', f'并发应返回 busy: {r2}'
        gate.set()
        r1v = await r1
        assert r1v.get('kind') == 'wrong_guess', r1v

    asyncio.run(driver())
finally:
    mod._check_guess = orig
    twentyq_guess.groups.pop(999, None)
    twentyq_guess._proc_locks.pop(999, None)

print('reason/lock tests passed')

# ── 定数档位语义：「是14吗」指 14.0~14.5 档，不是精确等于 14.0 ──
def _make_ds_music(purple_ds):
    bi = BasicInfo.model_validate({
        'title': 'D', 'artist': 'A', 'genre': '舞萌', 'bpm': 180,
        'release_date': '', 'from': 'maimai でらっくす', 'is_new': False,
        'version': 'maimai でらっくす',
    })
    return Music(
        id='9', title='D', type='SD', ds=[10.0, 12.0, 13.5, purple_ds],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1), charter='C') for _ in range(4)],
        basic_info=bi,
    )

# 14.4 问「是14吗」→ 应是（14 档 = 14.0~14.5），不是精确等于
ans, _, reason = classify_question(_make_ds_music(14.4), '紫谱是14吗')
assert ans == _YES, f'14.4 应属于 14 档: {ans}'
assert '14~14.5' in reason, reason
# 14.6 问「是14吗」→ 不是
ans, _, _ = classify_question(_make_ds_music(14.6), '紫谱是14吗')
assert ans == _NO, f'14.6 不属 14 档: {ans}'
# 14.4 问「14+」→ 不是（14+ = 14.6~15.0）
ans, _, reason = classify_question(_make_ds_music(14.4), '紫谱是14+吗')
assert ans == _NO, f'14.4 不属 14+: {ans}'
assert '14.6~15' in reason, reason
# 14.5 问「14+」→ 不是（14.5 < 14.6，属非+档上界）
ans, _, _ = classify_question(_make_ds_music(14.5), '紫谱是14+吗')
assert ans == _NO, f'14.5 不属 14+: {ans}'
# 14.6 问「14+」→ 是（14+ = 14.6~15.0）
ans, _, _ = classify_question(_make_ds_music(14.6), '紫谱是14+吗')
assert ans == _YES, f'14.6 属 14+: {ans}'
# 明确比较词「等于14」→ 精确相等，14.4 不等于 14.0 → 不是
ans, _, _ = classify_question(_make_ds_music(14.4), '紫谱定数等于14吗')
assert ans == _NO, f'等于应精确比较，14.4≠14: {ans}'

print('ds tier semantics tests passed')

# ── 分类（genre）规则匹配 ──
def _make_genre_music(genre, charter='サファ太'):
    bi = BasicInfo.model_validate({
        'title': 'G', 'artist': 'A', 'genre': genre, 'bpm': 180,
        'release_date': '', 'from': 'maimai でらっくす', 'is_new': False,
        'version': 'maimai でらっくす',
    })
    return Music(
        id='7', title='G', type='SD', ds=[10.0, 12.0, 13.6, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1), charter=charter) for _ in range(4)],
        basic_info=bi,
    )

# niconico → 术曲=是，原创曲=否
_g_nico = _make_genre_music('niconico＆ボーカロイド')  # 全角＆
_a, _c, _r = classify_question(_g_nico, '是术曲吗')
assert _c and _a == _YES, f'niconico 问术曲应回是: {_a}'
assert 'niconico' not in _r and 'ボーカロイド' not in _r, f'genre reason不应泄露: {_r}'
_a, _, _ = classify_question(_g_nico, '是原创曲吗')
assert _a == _NO, f'niconico 问原创应回不是: {_a}'
_a, _, _ = classify_question(_g_nico, '是联动曲吗')
assert _a == _YES, f'niconico 问联动应回是: {_a}'
# 半角& 格式同样匹配
_g_nico2 = _make_genre_music('niconico & VOCALOID')
_a, _c, _ = classify_question(_g_nico2, '是v家曲吗')
assert _c and _a == _YES, f'半角& niconico 问v家曲应回是: {_a}'

# maimai → 原创曲=是，联动曲=否
_g_mai = _make_genre_music('maimai')
_a, _, _ = classify_question(_g_mai, '是原创曲吗')
assert _a == _YES, f'maimai 问原创应回是: {_a}'
_a, _, _ = classify_question(_g_mai, '是联动曲吗')
assert _a == _NO, f'maimai 问联动应回不是: {_a}'
_a, _, _ = classify_question(_g_mai, '是术曲吗')
assert _a == _NO, f'maimai 问术曲应回不是: {_a}'

# POPS → 动漫曲=是
_g_pops = _make_genre_music('POPS＆アニメ')
_a, _, _ = classify_question(_g_pops, '是动漫曲吗')
assert _a == _YES, f'POPS 问动漫应回是: {_a}'
# 東方Project → 东方曲=是
_g_th = _make_genre_music('東方Project')
_a, _, _ = classify_question(_g_th, '是东方曲吗')
assert _a == _YES, f'東方 问东方应回是: {_a}'
# 宴会場 → 宴会曲=是
_g_ut = _make_genre_music('宴会場')
_a, _, _ = classify_question(_g_ut, '是宴会曲吗')
assert _a == _YES, f'宴会場 问宴会应回是: {_a}'

# 分类信息题 → unknown
assert classify_question(_g_nico, '是什么分类')[1] is False, '分类信息题应走 unknown'
assert classify_question(_g_nico, '什么genre')[1] is False, '分类信息题应走 unknown'
# 含定数关键词不抢答分类
assert classify_question(_g_nico, '紫谱定数是14吗')[1] is True, '定数题应由_q_ds回答'
assert '定数' in classify_question(_g_nico, '紫谱定数是14吗')[2]

print('genre rule tests passed')

# ── 谱师（charter）规则匹配 ──
# charter=サファ太 → 沙发太=是（别名），翠楼屋=否
_g_sf = _make_genre_music('maimai', charter='サファ太')
_a, _c, _r = classify_question(_g_sf, '谱师是沙发太吗')
assert _c and _a == _YES, f'サファ太 问沙发太应回是: {_a}'
assert 'サファ太' not in _r, f'charter reason不应泄露官方名: {_r}'
assert '沙发太' in _r, f'charter reason应回显玩家说法: {_r}'
_a, _, _ = classify_question(_g_sf, '谱师是翠楼屋吗')
assert _a == _NO, f'サファ太 问翠楼屋应回不是: {_a}'

# 错别字容错：普师事沙发太麻（普→谱、事→是、麻→吗）
_a, _c, _r = classify_question(_g_sf, '普师事沙发太麻')
assert _c and _a == _YES, f'错别字「普师事沙发太麻」应回是: {_a}'

# 用官方名也能匹配
_a, _, _ = classify_question(_g_sf, '谱师是サファ太吗')
assert _a == _YES, f'用官方名问应回是: {_a}'

# 别名 nyan → ニャイン
_g_ny = _make_genre_music('maimai', charter='ニャイン')
_a, _, _ = classify_question(_g_ny, '谱师是nyan吗')
assert _a == _YES, f'ニャイン 问nyan应回是: {_a}'
_a, _, _ = classify_question(_g_ny, '谱师是九条吗')
assert _a == _YES, f'ニャイン 问九条应回是: {_a}'

# 空格容忍（_norm 已去空格）
_a, _, _ = classify_question(_g_sf, '谱师 是 沙发太 吗')
assert _a == _YES, f'带空格也应匹配: {_a}'

# 谱师信息题 → unknown
assert classify_question(_g_sf, '谱师是谁')[1] is False, '谱师信息题应走 unknown'
assert classify_question(_g_sf, '是哪位谱师')[1] is False, '谱师信息题应走 unknown'
# 数量信息题（几首/多少）→ unknown
assert classify_question(_g_sf, '谱师写过几首')[1] is False, '数量信息题应走 unknown'
assert classify_question(_g_sf, '谱师有多少作品')[1] is False, '数量信息题应走 unknown'
# 无谱师署名
_g_no = _make_genre_music('maimai', charter='-')
_a, _c, _ = classify_question(_g_no, '谱师是沙发太吗')
assert _c and _a == _NO, f'无谱师署名应回不是: {_a}'

# 谱师属性/数量/主观是非题 → 走 LLM（不消耗，consumed=False）
# 历史 bug：「谱师写过的谱多吗」被 _q_charter 提取出 name='写过的谱多'，
# 匹配不到谱师后误回「不是喵」，应改为走 LLM 兜底。
for _prop_q in (
    '谱师写过的谱多吗',     # 数量是非题（用户实际触发 case）
    '谱师写过的歌少吗',     # 数量是非题
    '谱师是男的吗',         # 性别
    '谱师是女的吗',         # 性别
    '谱师是日本人吗',       # 国籍
    '谱师是中国人吗',       # 国籍
    '谱师有名吗',           # 知名度
    '谱师厉害吗',           # 主观
    '谱师写过别的谱吗',     # 产出属性
    '谱师还活着吗',         # 其他属性
):
    _a_p, _c_p, _ = classify_question(_g_sf, _prop_q)
    assert _c_p is False, f'属性题「{_prop_q}」应走 LLM 不消耗次数: consumed={_c_p}, ans={_a_p[:30]}'

# 正常名字匹配不受影响
_a, _c, _ = classify_question(_g_sf, '谱师是沙发太吗')
assert _c and _a == _YES, f'名字匹配仍应命中: {_a}'
_a, _c, _ = classify_question(_g_sf, '谱师是翠楼屋吗')
assert _c and _a == _NO, f'名字匹配不中应回不是: {_a}'

# 不抢答定数题
assert classify_question(_g_sf, '紫谱定数是14吗')[1] is True, '定数题不应被谱师handler抢答'

# ── _is_unanswerable_question：离谱题不走 LLM，不消耗次数 ──
from libraries.maimaidx_guess_20q import _is_unanswerable_question  # noqa: E402
# 谱师属性题
assert _is_unanswerable_question('谱师写过的谱多吗'), '谱师数量题应判为离谱题'
assert _is_unanswerable_question('谱师是男的吗'), '谱师性别题应判为离谱题'
assert _is_unanswerable_question('谱师是日本人吗'), '谱师国籍题应判为离谱题'
assert _is_unanswerable_question('谱师有名吗'), '谱师知名度题应判为离谱题'
# 艺术家属性题
assert _is_unanswerable_question('艺术家是女的吗'), '艺术家性别题应判为离谱题'
assert _is_unanswerable_question('曲师是中国人吗'), '艺术家国籍题应判为离谱题'
# 正常题不误判
assert not _is_unanswerable_question('谱师是沙发太吗'), '谱师名字匹配题不是离谱题'
assert not _is_unanswerable_question('BPM大于180吗'), 'BPM题不是离谱题'
assert not _is_unanswerable_question('紫谱定数是14吗'), '定数题不是离谱题'

# ── Luxizhel 别名匹配（官方名/罗马音）──
_g_lx = _make_genre_music('maimai', charter='Luxizhel')
_a, _c, _ = classify_question(_g_lx, '谱师是luxizhel吗')
assert _c and _a == _YES, f'Luxizhel 问luxizhel应回是: {_a}'
_a, _c, _ = classify_question(_g_lx, '谱师是Luxizhel吗')
assert _c and _a == _YES, f'Luxizhel 问Luxizhel应回是: {_a}'

# ── サファ太马甲 Safata.Hz / Safata.GHz + 俗称 翠 ──
# 曲目谱师=サファ太，玩家用马甲署名提问应回是（同一个人换皮写谱）
_g_sf = _make_genre_music('maimai', charter='サファ太')
for _alias_q in ('谱师是Safata.Hz吗', '谱师是safata.hz吗', '谱师是Safata.GHz吗', '谱师是safatahz吗', '谱师是翠吗'):
    _a, _c, _ = classify_question(_g_sf, _alias_q)
    assert _c and _a == _YES, f'サファ太 问「{_alias_q}」应回是: {_a}'

# ── 合作名 safaTAmago = サファ太 + 玉子豆腐（FFT MASTER 署名）──
# 双向匹配：曲署名是合作名时，问任一参与方都应回是；反之亦然
_g_co = _make_genre_music('maimai', charter='safaTAmago')
for _alias_q in ('谱师是サファ太吗', '谱师是沙发太吗', '谱师是玉子豆腐吗'):
    _a, _c, _ = classify_question(_g_co, _alias_q)
    assert _c and _a == _YES, f'合作名safaTAmago 问「{_alias_q}」应回是: {_a}'
# 反向：曲署名是单方，问合作名也回是（该方参与了合作）
_g_sf2 = _make_genre_music('maimai', charter='サファ太')
_a, _c, _ = classify_question(_g_sf2, '谱师是safatamago吗')
assert _c and _a == _YES, f'サファ太 问safatamago应回是: {_a}'
_g_tk2 = _make_genre_music('maimai', charter='玉子豆腐')
_a, _c, _ = classify_question(_g_tk2, '谱师是safatamago吗')
assert _c and _a == _YES, f'玉子豆腐 问safatamago应回是: {_a}'

# ── はっぴー马甲 緑風 犬三郎 / 原田ひろゆき ──
_g_hp = _make_genre_music('maimai', charter='はっぴー')
for _alias_q in ('谱师是緑風 犬三郎吗', '谱师是绿风犬三郎吗', '谱师是原田ひろゆき吗', '谱师是哈皮吗'):
    _a, _c, _ = classify_question(_g_hp, _alias_q)
    assert _c and _a == _YES, f'はっぴー 问「{_alias_q}」应回是: {_a}'

# ── シチミヘルツ马甲 7.3Hz / 7.3GHz ──
_g_sc = _make_genre_music('maimai', charter='シチミヘルツ')
for _alias_q in ('谱师是7.3Hz吗', '谱师是7.3GHz吗', '谱师是7.3吗'):
    _a, _c, _ = classify_question(_g_sc, _alias_q)
    assert _c and _a == _YES, f'シチミヘルツ 问「{_alias_q}」应回是: {_a}'

# ── 小鳥遊さん马甲 Phoenix ──
_g_tk = _make_genre_music('maimai', charter='小鳥遊さん')
for _alias_q in ('谱师是Phoenix吗', '谱师是phoenix吗', '谱师是小鸟游吗', '谱师是takanashi吗'):
    _a, _c, _ = classify_question(_g_tk, _alias_q)
    assert _c and _a == _YES, f'小鳥遊さん 问「{_alias_q}」应回是: {_a}'

print('charter rule tests passed')

# ── 比较词覆盖测试（紫谱=14.6）──
_cmp_cases = [
    # ≥ 类（GE）—— 14.6 应满足 ≥14
    ('紫谱定数至少14吗', _YES, '≥', 14),
    ('紫谱定数起码14吗', _YES, '≥', 14),
    ('紫谱定数最少14吗', _YES, '≥', 14),
    ('紫谱定数不低于14吗', _YES, '≥', 14),
    ('紫谱定数不小于14吗', _YES, '≥', 14),
    ('紫谱定数大於等於14吗', _YES, '≥', 14),
    ('紫谱定数≧14吗', _YES, '≥', 14),
    # ≤ 类（LE）—— 14.6 不满足 ≤14
    ('紫谱定数最多14吗', _NO, '≤', 14),
    ('紫谱定数至多14吗', _NO, '≤', 14),
    ('紫谱定数不高于14吗', _NO, '≤', 14),
    ('紫谱定数不超过14吗', _NO, '≤', 14),
    ('紫谱定数不大於14吗', _NO, '≤', 14),
    ('紫谱定数小於等於14吗', _NO, '≤', 14),
    ('紫谱定数≦14吗', _NO, '≤', 14),
    # > 类（GT）—— 14.6 满足 >14
    ('紫谱定数大于14吗', _YES, '>', 14),
    ('紫谱定数大於14吗', _YES, '>', 14),
    ('紫谱定数超过14吗', _YES, '>', 14),
    ('紫谱定数超過14吗', _YES, '>', 14),
    ('紫谱定数高于14吗', _YES, '>', 14),
    ('紫谱定数高於14吗', _YES, '>', 14),
    ('紫谱定数多过14吗', _YES, '>', 14),
    ('紫谱定数多于14吗', _YES, '>', 14),
    ('紫谱定数多於14吗', _YES, '>', 14),
    ('紫谱定数超出14吗', _YES, '>', 14),
    ('紫谱定数＞14吗', _YES, '>', 14),
    # < 类（LT）—— 14.6 不满足 <14
    ('紫谱定数小于14吗', _NO, '<', 14),
    ('紫谱定数小於14吗', _NO, '<', 14),
    ('紫谱定数低于14吗', _NO, '<', 14),
    ('紫谱定数低於14吗', _NO, '<', 14),
    ('紫谱定数不到14吗', _NO, '<', 14),
    ('紫谱定数不满14吗', _NO, '<', 14),
    ('紫谱定数不滿14吗', _NO, '<', 14),
    ('紫谱定数少于14吗', _NO, '<', 14),
    ('紫谱定数少於14吗', _NO, '<', 14),
    ('紫谱定数不足14吗', _NO, '<', 14),
    ('紫谱定数没到14吗', _NO, '<', 14),
    ('紫谱定数沒到14吗', _NO, '<', 14),
    ('紫谱定数未到14吗', _NO, '<', 14),
    ('紫谱定数未满14吗', _NO, '<', 14),
    ('紫谱定数未滿14吗', _NO, '<', 14),
    ('紫谱定数＜14吗', _NO, '<', 14),
    # = 类（EQ）
    ('紫谱定数等于14.6吗', _YES, '=', 14.6),
    ('紫谱定数等於14.6吗', _YES, '=', 14.6),
]
for _q, _exp, _sym, _val in _cmp_cases:
    _a, _c, _r = classify_question(m, _q)
    assert _c, f'比较词题应命中: {_q}'
    assert _a == _exp, f'{_q} → 期望{_exp} 实际{_a}（紫谱=14.6 {_sym}{_val}）'
    # reason 应显示对应的比较符号
    assert _sym in _r or str(_val) in _r, f'reason 缺少 {_sym}/{_val}: {_r}'

print('comparison words tests passed')

# ── 「比 X 大/小」句式测试（紫谱=14.6）──
_bi_cases = [
    # 比 X 小/低/少 → <（14.6 不满足 <14）
    ('紫谱定数比13小吗', _NO, 13),
    ('定数比13小吗', _NO, 13),  # 无颜色默认紫谱
    ('紫谱定数比14小吗', _NO, 14),
    ('紫谱定数比13低吗', _NO, 13),
    ('紫谱定数比13少吗', _NO, 13),
    # 比 X 大/高/多 → >（14.6 满足 >14）
    ('紫谱定数比13大吗', _YES, 13),
    ('定数比13大吗', _YES, 13),  # 无颜色默认紫谱
    ('紫谱定数比14大吗', _YES, 14),
    ('紫谱定数比13高吗', _YES, 13),
    ('紫谱定数比13多吗', _YES, 13),
    # 形容词在前语序：比大13 / 比小13
    ('紫谱定数比大13吗', _YES, 13),
    ('紫谱定数比小13吗', _NO, 13),
]
for _q, _exp, _val in _bi_cases:
    _a, _c, _r = classify_question(m, _q)
    assert _c, f'比字句应命中: {_q}'
    assert _a == _exp, f'{_q} → 期望{_exp} 实际{_a}（紫谱=14.6）'
    # reason 应显示 > 或 < 符号
    assert ('>' in _r or '<' in _r), f'reason 缺少比较符号: {_r}'

print('bi-comparison (比X大/小) tests passed')

# ── 版本（version）规则匹配 ──
def _make_ver_music(version):
    bi = BasicInfo.model_validate({
        'title': 'V', 'artist': 'A', 'genre': 'maimai', 'bpm': 180,
        'release_date': '', 'from': version, 'is_new': False, 'version': version,
    })
    return Music(
        id='9', title='V', type='SD', ds=[10.0, 12.0, 13.6, 14.6],
        level=['7', '10', '12+', '13+'], cids=[1, 2, 3, 4],
        charts=[Chart(notes=namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1), charter='x') for _ in range(4)],
        basic_info=bi,
    )

# buddies（双代）→ 是双代=是，是宴代(PLUS)=不是，是双宴代=是
_v_buddies = _make_ver_music('maimai でらっくす buddies')
_a, _c, _r = classify_question(_v_buddies, '是双代吗')
assert _c and _a == _YES, f'buddies 问双代应回是: {_a}'
assert 'buddies' not in _r.lower(), f'version reason不应泄露官方名: {_r}'
_a, _, _ = classify_question(_v_buddies, '是宴代吗')
assert _a == _NO, f'buddies 问宴代(PLUS)应回不是: {_a}'
_a, _, _ = classify_question(_v_buddies, '是双宴代吗')
assert _a == _YES, f'buddies 问双宴代应回是: {_a}'

# buddies plus（宴代）→ 是宴代=是，是双代=不是
_v_bp = _make_ver_music('maimai でらっくす buddies plus')
_a, _, _ = classify_question(_v_bp, '是宴代吗')
assert _a == _YES, f'buddies plus 问宴代应回是: {_a}'
_a, _, _ = classify_question(_v_bp, '是双代吗')
assert _a == _NO, f'buddies plus 问双代应回不是: {_a}'

# でらっくす（熊代）→ 是熊代=是，是华代(PLUS)=不是，是熊华代=是
_v_dx = _make_ver_music('maimai でらっくす')
_a, _, _ = classify_question(_v_dx, '是熊代吗')
assert _a == _YES, f'でらっくす 问熊代应回是: {_a}'
_a, _, _ = classify_question(_v_dx, '是华代吗')
assert _a == _NO, f'でらっくす 问华代(PLUS)应回不是: {_a}'
_a, _, _ = classify_question(_v_dx, '是熊华代吗')
assert _a == _YES, f'でらっくす 问熊华代应回是: {_a}'

# green plus → 是檄代=是（PLUS 别名），是超代=不是（基版）
_v_gp = _make_ver_music('maimai green plus')
_a, _, _ = classify_question(_v_gp, '是檄代吗')
assert _a == _YES, f'green plus 问檄代应回是: {_a}'
_a, _, _ = classify_question(_v_gp, '是超代吗')
assert _a == _NO, f'green plus 问超代(基版)应回不是: {_a}'

# green（超代/绿代）→ 是超代=是，是檄代(PLUS)=不是
_v_g = _make_ver_music('maimai green')
_a, _, _ = classify_question(_v_g, '是超代吗')
assert _a == _YES, f'green 问超代应回是: {_a}'
_a, _, _ = classify_question(_v_g, '是檄代吗')
assert _a == _NO, f'green 问檄代(PLUS)应回不是: {_a}'

# 大小写+空格容忍：Green Plus / greenplus / GREEN PLUS 都匹配
_a, _, _ = classify_question(_v_gp, '是Green Plus吗')
assert _a == _YES, f'Green Plus 大小写应匹配: {_a}'
_a, _, _ = classify_question(_v_gp, '是greenplus吗')
assert _a == _YES, f'greenplus 无空格应匹配: {_a}'

# 旧框 → 是舞代=是
_v_old = _make_ver_music('maimai milk')
_a, _, _ = classify_question(_v_old, '是舞代吗')
assert _a == _YES, f'milk(旧框) 问舞代应回是: {_a}'
# 新框 → 是舞代=不是
_a, _, _ = classify_question(_v_dx, '是舞代吗')
assert _a == _NO, f'でらっくす(新框) 问舞代应回不是: {_a}'

# 版本信息题 → unknown
assert classify_question(_v_buddies, '是什么版本')[1] is False, '版本信息题应走 unknown'
assert classify_question(_v_buddies, '哪一代')[1] is False, '版本信息题应走 unknown'
# 版本顺序题 → 仍走 LLM
assert classify_question(_v_buddies, '在雪代及以后吗')[1] is False, '版本顺序题应走 LLM'
assert classify_question(_v_buddies, '比双代更早吗')[1] is False, '版本顺序题应走 LLM'

print('version rule tests passed')

# ── 区间题 + 「以下」类测试（紫谱=14.6）──
# 「定数是14以下的吗」→ 以下 → ≤14 → 14.6 不满足 → 不是
_a, _c, _r = classify_question(m, '定数是14以下的吗')
assert _c and _a == _NO, f'14以下 14.6应回不是: {_a}'
assert '≤' in _r or '14' in _r, f'reason 应含≤/14: {_r}'

# 「是13.6-14.0的吗」→ 两数字无比较词 → 区间判断 13.6≤v≤14.0
_a, _c, _r = classify_question(m, '是13.6-14.0的吗')
assert _c, f'区间题应命中: consumed={_c}'
assert _a == _NO, f'13.6-14.0 紫谱14.6不在区间应回不是: {_a}'
assert '13.6' in _r and '14' in _r, f'reason应含区间: {_r}'

# 紫谱在区间内的情况：14.5-14.7 → 14.6 在区间 → 是
_a, _, _ = classify_question(m, '紫谱定数在14.5-14.7之间吗')
assert _a == _YES, f'14.5-14.7 紫谱14.6在区间应回是: {_a}'

# 区间用「到」「至」连接也支持（_norm 不变，数字提取正常）
_a, _, _ = classify_question(m, '紫谱定数14.5到14.7吗')
assert _a == _YES, f'14.5到14.7 应回是: {_a}'

print('range & le tests passed')
