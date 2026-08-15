#!/usr/bin/env python3
"""采样 v.wmc.pub 谱面标签，汇总全部出现的标签词表，校验/补全你想我猜标签词表。

用途（对应需求「随机抽几首歌调一下 api，看看都有啥标签，做一个汇总」）：
  1. 随机抽 N 首曲，拉取各难度 WMC 标签（评价/配置/模式/难度分类）。
  2. 聚合所有出现的 label，按字段分组输出 JSON。
  3. 与 libraries.maimaidx_guess_20q._WMC_TAG_VOCAB 交叉比对：
     - 词表里哪些 label 在样本中出现（覆盖率）
     - 样本里有哪些 label 不在词表中（待补全的缺口）

运行环境：需能访问 v.wmc.pub 的 wmc_api_key（在你部署环境里）。
  --key / 环境变量 WMC_API_KEY    API Bearer 令牌
  --base-url                      默认 https://v.wmc.pub/api/v1
  --dxdata                        dxdata.json 路径（默认仓库根）
  --sample                        抽样数量（默认 20）
  --seed                          随机种子（默认 123，保证可复现）
  --out                           输出 JSON 路径（默认 scripts/wmc_tags_summary.json）

注意：v.wmc.pub 有 5 小时配额限流；若返回 429，脚本会自动等待重置后重试。
"""
import argparse
import json
import os
import random
import sys
import time
import types
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _inject_music_stub():
    """注入 libraries.maimaidx_music 的轻量 stub，避免触发 NoneBot 配置。"""
    import importlib
    model_mod = importlib.import_module('libraries.maimaidx_model')
    stub = types.ModuleType('libraries.maimaidx_music')
    stub.Music = model_mod.Music
    stub.mai = types.SimpleNamespace()
    stub.guess = types.SimpleNamespace()
    sys.modules['libraries.maimaidx_music'] = stub


def _load_vocab():
    """读取你想我猜的标签词表（单一事实来源）。"""
    _inject_music_stub()
    from libraries.maimaidx_guess_20q import _WMC_TAG_VOCAB
    return _WMC_TAG_VOCAB


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--key', default=os.environ.get('WMC_API_KEY', ''),
                    help='v.wmc.pub API Bearer 令牌（或用环境变量 WMC_API_KEY）')
    ap.add_argument('--base-url', default='https://v.wmc.pub/api/v1')
    ap.add_argument('--dxdata', default=str(ROOT / 'dxdata.json'))
    ap.add_argument('--sample', type=int, default=20)
    ap.add_argument('--seed', type=int, default=123)
    ap.add_argument('--out', default=str(ROOT / 'scripts' / 'wmc_tags_summary.json'))
    args = ap.parse_args()

    if not args.key:
        print('[错误] 未提供 wmc_api_key（--key 或环境变量 WMC_API_KEY）。'
              '该脚本需要访问 v.wmc.pub。', file=sys.stderr)
        sys.exit(2)

    import httpx
    from libraries.maimaidx_wmc_api import (
        make_chart_key, song_id_for_wmc, kind_for_wmc, diff_value_for_wmc,
    )

    # 载入曲目库
    with open(args.dxdata, 'r', encoding='utf-8') as f:
        songs = json.load(f)
    print(f'[info] 曲库共 {len(songs)} 首，抽样 {args.sample} 首（seed={args.seed}）')

    random.seed(args.seed)
    sampled = random.sample(songs, min(args.sample, len(songs)))

    field_labels: dict[str, Counter] = defaultdict(Counter)
    seen_chart_keys = 0
    errors = 0

    headers = {'Authorization': f'Bearer {args.key}'}
    base = args.base_url.rstrip('/')

    for s in sampled:
        sid = song_id_for_wmc(s)
        kind = kind_for_wmc(s)
        ds = s.get('ds') or []
        for i in range(min(len(ds), 5)):
            dval = diff_value_for_wmc(i)
            ck = make_chart_key(sid, kind, dval)
            url = f'{base}/charts/{ck}/tags'
            for attempt in range(4):
                try:
                    r = httpx.get(url, headers=headers,
                                  params={'radar_threshold': 40, 'feature_threshold': 0.5},
                                  timeout=15.0)
                except Exception as e:  # noqa: BLE001
                    print(f'[warn] 请求异常 {ck}: {e}', file=sys.stderr)
                    errors += 1
                    break
                if r.status_code == 429:
                    # 配额限流：等待重置（最多 ~6 小时窗口里取一次长等待）
                    wait = 60 * (attempt + 1) * 5
                    print(f'[429] 配额限流，{wait}s 后重试 {ck}（attempt {attempt+1}）', file=sys.stderr)
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    errors += 1
                    break
                data = r.json() or {}
                tags = data.get('tags') if isinstance(data, dict) else None
                if not tags:
                    break
                seen_chart_keys += 1
                for lbl in (tags.get('evaluationTags') or []):
                    if lbl.get('label'):
                        field_labels['evaluationTags'][lbl['label']] += 1
                for lbl in (tags.get('radarTags') or []):
                    if lbl.get('label'):
                        field_labels['radarTags'][lbl['label']] += 1
                for lbl in (tags.get('patterns') or []):
                    if lbl.get('label'):
                        field_labels['patterns'][lbl['label']] += 1
                dc = tags.get('difficultyClassification') or {}
                if dc.get('label'):
                    field_labels['difficultyClassification'][dc['label']] += 1
                break

    summary = {
        'sampled_songs': len(sampled),
        'chart_keys_with_tags': seen_chart_keys,
        'errors': errors,
        'field_labels': {k: dict(v.most_common()) for k, v in field_labels.items()},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f'[ok] 已写入标签汇总：{args.out}')

    # ── 与词表交叉比对 ──
    print('\n═══ 词表覆盖率 / 缺口报告 ═══')
    vocab = _load_vocab()
    # 词表里每个条目可能匹配多个字段；把条目里出现过的子串收集起来
    vocab_labels: set[str] = set()
    for name, _triggers, matchers in vocab:
        vocab_labels.add(name)
        for _field, sub in matchers:
            vocab_labels.add(sub)

    # 样本里出现的所有 label（跨字段）
    sampled_labels: set[str] = set()
    for labels in field_labels.values():
        sampled_labels.update(labels.keys())

    # 覆盖率：词表子串里，能在样本 label 中被「子串命中」的（宽松匹配，兼容 星星/星星谱 这类）
    covered = set()
    for v in vocab_labels:
        for s in sampled_labels:
            if v and v.lower() in s.lower():
                covered.add(v)
                break
    print(f'词表条目/子串数：{len(vocab_labels)}，样本中出现并能命中：{len(covered)}')

    # 缺口：样本里有、但词表任何子串都没命中的 label
    gaps = []
    for s in sorted(sampled_labels):
        hit = any(v and v.lower() in s.lower() for v in vocab_labels)
        if not hit:
            gaps.append(s)
    if gaps:
        print(f'\n[缺口] 样本中存在但词表未覆盖的标签（建议补全）：')
        for g in gaps:
            print(f'   - {g}')
    else:
        print('\n[缺口] 无——样本中所有标签都已被词表覆盖 ✅')

    # 反向：词表里哪些子串在样本里完全没出现（可能是冷门/过期标签，供核查）
    unused = [v for v in sorted(vocab_labels) if v not in covered]
    if unused:
        print(f'\n[待核查] 词表里有但本次样本未出现的子串（可能冷门/需核对）：')
        for u in unused:
            print(f'   - {u}')


if __name__ == '__main__':
    main()
