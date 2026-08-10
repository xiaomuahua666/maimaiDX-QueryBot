"""20问：基于真实曲库（dxdata.json）的规则层回归测试。

随机选 N 首曲，针对每首曲自动生成多维度是非题（分类/版本/版本顺序/定数/谱面类型/BPM），
根据曲目真实特征算出「期望答案」，再与 classify_question 的实际返回对比，统计准确率。

只测规则层（consumed=True 的题），不测 LLM 兜底（LLM 需联网且不可靠）。
"""
import asyncio
import json
import random
import sys
import types
from pathlib import Path
from collections import namedtuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── stub maimaidx_music（避免触发完整 NoneBot 生态加载）──
import importlib
model_mod = importlib.import_module('libraries.maimaidx_model')
music_stub = types.ModuleType('libraries.maimaidx_music')


class _AliasList(list):
    def by_alias(self, music_alias):
        return []


class _MaiStub:
    total_alias_list = _AliasList()


music_stub.mai = _MaiStub()
music_stub.guess = types.SimpleNamespace()
music_stub.Music = model_mod.Music
sys.modules['libraries.maimaidx_music'] = music_stub

from libraries.maimaidx_guess_20q import classify_question, _YES, _NO  # noqa: E402
from libraries.maimaidx_model import BasicInfo, Chart, Music  # noqa: E402

# ── 从 dxdata.json 加载真实曲目，构造 Music 对象 ──
with open(ROOT / 'dxdata.json', 'r', encoding='utf-8') as f:
    _DX = json.load(f)

# 官方 version 字段 → 曲目 basic_info.version 用的完整字符串（与 _VERSION_KEYWORDS 一致）
# dxdata 里 sheets[].version 形如 "BUDDiES PLUS" / "maimaiでらっくす" / "MiLK" 等
# 需归一化到 _VERSION_KEYWORDS 里的 canonical（小写）
_VER_NAME_MAP = {
    'maimai': 'maimai',
    'maimai PLUS': 'maimai plus',
    'GreeN': 'maimai green',
    'GreeN PLUS': 'maimai green plus',
    'ORANGE': 'maimai orange',
    'ORANGE PLUS': 'maimai orange plus',
    'PiNK': 'maimai pink',
    'PiNK PLUS': 'maimai pink plus',
    'MURASAKi': 'maimai murasaki',
    'MURASAKi PLUS': 'maimai murasaki plus',
    'MiLK': 'maimai milk',
    'MiLK PLUS': 'maimai milk plus',
    'FiNALE': 'maimai finale',
    'maimaiでらっくす': 'maimai でらっくす',
    'maimaiでらっくす PLUS': 'maimai でらっくす plus',
    'Splash': 'maimai でらっくす splash',
    'Splash PLUS': 'maimai でらっくす splash plus',
    'UNiVERSE': 'maimai でらっくす universe',
    'UNiVERSE PLUS': 'maimai でらっくす universe plus',
    'FESTiVAL': 'maimai でらっくす festival',
    'FESTiVAL PLUS': 'maimai でらっくす festival plus',
    'BUDDiES': 'maimai でらっくす buddies',
    'BUDDiES PLUS': 'maimai でらっくす buddies plus',
    'PRiSM': 'maimai でらっくす prism',
    'PRiSM PLUS': 'maimai でらっくす prism plus',
    'CiRCLE': 'maimai でらっくす circle',
    'CiRCLE PLUS': 'maimai でらっくす circle plus',
}


def _normalize_version(v: str) -> str:
    return _VER_NAME_MAP.get(v, v.lower())


def _build_music_from_dx(song: dict) -> Music:
    sheets = song.get('sheets', [])
    # 取四个难度的定数：basic/advanced/expert/master
    diff_order = ['basic', 'advanced', 'expert', 'master']
    sheets_by_diff = {s['difficulty']: s for s in sheets}
    ds_list = []
    level_list = []
    cid_list = []
    charter_list = []
    for d in diff_order:
        s = sheets_by_diff.get(d)
        if s:
            ds_list.append(float(s.get('internalLevelValue', 0)))
            level_list.append(s.get('level', '?'))
            cid_list.append(s.get('internalId', 0))
            charter_list.append(s.get('noteDesigner', '-') or '-')
        else:
            ds_list.append(0.0)
            level_list.append('?')
            cid_list.append(0)
            charter_list.append('-')
    # 版本：取该曲最早出现的版本（sheets 里第一个 version）
    raw_ver = sheets[0]['version'] if sheets else ''
    version = _normalize_version(raw_ver)
    notes = namedtuple('N', ['tap', 'hold', 'slide', 'brk'])(1, 1, 1, 1)
    bi = BasicInfo.model_validate({
        'title': song.get('title', ''),
        'artist': song.get('artist', ''),
        'genre': song.get('category', ''),
        'bpm': song.get('bpm', 0),
        'release_date': '',
        'from': version,
        'is_new': song.get('isNew', False),
        'version': version,
    })
    return Music(
        id=str(song.get('songId', '')),
        title=song.get('title', ''),
        type='SD',
        ds=ds_list,
        level=level_list,
        cids=cid_list,
        charts=[Chart(notes=notes, charter=c) for c in charter_list],
        basic_info=bi,
    )


# ── 加载曲目池，过滤掉缺数据的 ──
_all_songs = _DX['songs']
_musics = []
for s in _all_songs:
    try:
        m = _build_music_from_dx(s)
        if m.ds and m.basic_info.version and m.basic_info.genre:
            _musics.append(m)
    except Exception:
        continue

print(f'加载真实曲目: {len(_musics)} 首')
print(f'版本分布:')
from collections import Counter
_ver_dist = Counter(m.basic_info.version for m in _musics)
for v, c in sorted(_ver_dist.items()):
    print(f'  {v}: {c}')
_genre_dist = Counter(m.basic_info.genre for m in _musics)
print(f'分类分布:')
for g, c in sorted(_genre_dist.items(), key=lambda x: -x[1]):
    print(f'  {g}: {c}')

# ── 测试用例生成器：根据曲目真实特征生成是非题 + 期望答案 ──

def _genre_key(m: Music) -> str:
    g = (m.basic_info.genre or '').lower().replace(' ', '').replace('　', '')
    # 全角片假名 → 关键词匹配
    if 'maimai' == g:
        return 'maimai'
    if 'niconico' in g or 'ボーカロイド' in g or 'vocaloid' in g:
        return 'niconico'
    if 'touhou' in g or '東方' in g or '东方' in g:
        return 'touhou'
    if 'pops' in g or 'アニメ' in g or 'anime' in g:
        return 'pops'
    if 'ゲーム' in g or 'game' in g or 'バラエティ' in g:
        return 'game'
    if 'オンゲキ' in g or 'ongeki' in g or 'chunithm' in g or 'チュウニズム' in g:
        return 'ongeki'
    if '宴会' in g or 'utage' in g:
        return 'utage'
    return ''

# 版本索引（用于版本顺序期望答案计算）
import libraries.maimaidx_guess_20q as mod
_VER_IDX = mod._VERSION_INDEX  # canonical(lower) -> idx
_VER_KW = mod._VERSION_KEYWORDS  # (canonical, kws)
_GRP = mod._VERSION_GROUP_ALIASES  # name -> (lo_canonical, hi_canonical)


def _ver_idx_of(m: Music) -> int:
    """用生产代码的 _music_version_index 查索引，确保归一化一致。"""
    idx = mod._music_version_index(m)
    return idx if idx is not None else -1


def _gen_cases(m: Music):
    """针对一首曲生成 (问题, 期望是/否) 列表。只生成规则层应命中的题。"""
    cases = []
    gi = _genre_key(m)
    vi = _ver_idx_of(m)

    # ── 分类题 ──
    # 「是舞萌吗」→ 分类==maimai
    cases.append(('是舞萌吗', gi == 'maimai'))
    cases.append(('是原创曲吗', gi == 'maimai'))
    cases.append(('是术曲吗', gi == 'niconico'))
    cases.append(('是东方曲吗', gi == 'touhou'))
    cases.append(('是动漫曲吗', gi == 'pops'))
    cases.append(('是宴会曲吗', gi == 'utage'))
    # 联动曲：非 maimai
    cases.append(('是联动曲吗', gi != '' and gi != 'maimai'))

    # ── 版本俗称题（只测能稳定映射的）──
    # 取该曲版本对应的俗称，问「是X代吗」
    v_lower = (m.basic_info.version or '').lower()
    for canonical, kws in _VER_KW:
        if canonical == v_lower:
            # 用第一个中文俗称提问
            cn_kw = None
            for kw in kws:
                if any(ord(ch) > 127 for ch in kw):  # 含中文字符
                    cn_kw = kw
                    break
            if cn_kw:
                cases.append((f'是{cn_kw}吗', True))
            break

    # ── 版本顺序题 ──
    if vi >= 0:
        # 雪代 idx=11
        if vi >= 11:
            cases.append(('在雪代及以后吗', True))
        else:
            cases.append(('在雪代及以后吗', False))
        # 双代 idx=21
        if vi >= 21:
            cases.append(('在双代及以后吗', True))
        else:
            cases.append(('在双代及以后吗', False))
        # 之前
        if vi < 11:
            cases.append(('在雪代之前吗', True))
        else:
            cases.append(('在雪代之前吗', False))
        # 合并叫法：双宴代=[21,22]
        if 21 <= vi <= 22:
            cases.append(('是双宴代吗', True))
        else:
            cases.append(('是双宴代吗', False))
        # 比X早/晚
        if vi < 11:
            cases.append(('比雪代早吗', True))
            cases.append(('比雪代晚吗', False))
        elif vi == 11:
            cases.append(('比雪代早吗', False))
            cases.append(('比雪代晚吗', False))
        else:
            cases.append(('比雪代早吗', False))
            cases.append(('比雪代晚吗', True))
        # 舞代（旧框 0-12）
        cases.append(('是舞代吗', 0 <= vi <= 12))

    # ── 定数题（紫谱，即 master）──
    if len(m.ds) >= 4 and m.ds[3] > 0:
        purple_ds = m.ds[3]
        # 是 N 档（整数部分）
        n = int(purple_ds)
        if n > 0:
            # 13.5 问「是13吗」→ 是（13档=[13.0,13.5]）
            cases.append((f'紫谱是{n}吗', n <= purple_ds < n + 0.6))
            # 13.5 问「是13+吗」→ 否（13+=[13.6,13.9]，13.5 不在）
            cases.append((f'紫谱是{n}+吗', n + 0.6 <= purple_ds < n + 1.0))
        # 小数定数精确等于
        if purple_ds != int(purple_ds):
            cases.append((f'紫谱定数{purple_ds:g}吗', True))
            # 问一个错误的小数定数
            wrong_ds = purple_ds + 0.1 if int((purple_ds % 1) * 10) < 9 else purple_ds - 0.1
            cases.append((f'紫谱定数{wrong_ds:g}吗', False))

    # ── 谱面类型题 ──
    # SD/标准谱面（type='SD'）vs DX 谱面——按 sheets 第一个 type 判断
    # 这里 stub 全设 SD，跳过

    # ── BPM 题 ──
    bpm = m.basic_info.bpm
    if bpm > 0:
        cases.append((f'BPM大于{bpm - 1}吗', True))  # bpm > bpm-1
        cases.append((f'BPM大于{bpm + 1}吗', False))  # bpm > bpm+1 = False
        cases.append((f'BPM大于等于{bpm}吗', True))

    return cases


# ── 跑回归 ──
random.seed(42)
sample = random.sample(_musics, min(200, len(_musics)))

total = 0
passed = 0
failed_cases = []

for m in sample:
    cases = _gen_cases(m)
    for q, expected in cases:
        total += 1
        try:
            ans, consumed, reason = classify_question(m, q)
        except Exception as e:
            failed_cases.append((m, q, expected, 'EXC', str(e)))
            continue
        if not consumed:
            # 规则层未命中，跳过（不计入失败，但记录）
            failed_cases.append((m, q, expected, 'NOT_CONSUMED', reason))
            continue
        expected_ans = _YES if expected else _NO
        if ans == expected_ans:
            passed += 1
        else:
            failed_cases.append((m, q, expected, ans, reason))

print()
print('=' * 70)
print(f'回归结果: {passed}/{total} 通过 ({passed/total*100:.1f}%)')
not_consumed = [f for f in failed_cases if f[3] == 'NOT_CONSUMED']
real_fail = [f for f in failed_cases if f[3] not in ('NOT_CONSUMED', 'EXC')]
exc_fail = [f for f in failed_cases if f[3] == 'EXC']
print(f'  规则未命中(跳过): {len(not_consumed)}')
print(f'  异常: {len(exc_fail)}')
print(f'  真实错误: {len(real_fail)}')

if real_fail:
    print()
    print('=== 真实错误详情（前20条）===')
    for m, q, exp, ans, reason in real_fail[:20]:
        gi = _genre_key(m)
        vi = _ver_idx_of(m)
        purple = m.ds[3] if len(m.ds) >= 4 else 0
        print(f'  曲:{m.title[:20]} | genre={gi} ver={m.basic_info.version}({vi}) purple_ds={purple:g}')
        print(f'    Q: {q!r} 期望={_YES if exp else _NO} 实际={ans}')
        print(f'    reason: {reason!r}')

if not_consumed:
    print()
    print(f'=== 规则未命中（前10条，这些题走了 LLM，未计入错误）===')
    for m, q, exp, _, reason in not_consumed[:10]:
        print(f'  曲:{m.title[:20]} Q:{q!r}')

print()
if not real_fail and not exc_fail:
    print('✓ 全部规则层判定正确，无错误')
else:
    print(f'✗ 有 {len(real_fail)} 条错误，需修复')
    sys.exit(1)
