#!/usr/bin/env python3
"""
排行榜 / 报告图片渲染回归测试。

覆盖目标：
  1. 五类渲染图（群 Rating 榜、单曲榜、吃分榜、寸止/锁血榜、今日吃分推荐、日报/周报）
     在「无任何游戏素材」环境下能正常出图且不抛异常（验证缺失回退）。
  2. 在「存在假游戏贴图」环境下，评级 / Rating 徽章贴图能被加载，
     且 draw_rating_badge 返回宽度 == rating_badge_width()，保证徽章右对齐不错位。
  3. 曲绘占位图为正方形；源码层面保证曲绘不被圆角裁剪。
  4. 宴会场谱面（song_id >= 100000）在报告 B50 构建中被过滤。
  5. 群 Rating 榜 all_rows 统计全群数据（行数与显示行数解耦）。

CI 环境没有生产字体 / 贴图，本测试自建临时静态目录并通过 fake config 注入，
不依赖 nonebot 运行时。
"""
from __future__ import annotations

import asyncio
import ast
import importlib.util
import logging
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
WIDTH = 1080


# ----------------------------------------------------------------------
# 隔离加载 libraries 渲染模块（注入 fake config，避免拉起 nonebot 运行时）
# ----------------------------------------------------------------------
def _load_render_modules(static_dir: Path):
    pic_dir = static_dir / "mai" / "pic"
    cover_dir = static_dir / "mai" / "cover"
    font_dir = static_dir / "font"
    pic_dir.mkdir(parents=True, exist_ok=True)
    cover_dir.mkdir(parents=True, exist_ok=True)
    font_dir.mkdir(parents=True, exist_ok=True)

    # 一个最小可用的 TrueType（PIL 默认位图字体无法缩放，用仓库自带字体若存在）
    cand_fonts = [
        ROOT / "GenSenMaruGothicTW-Regular.ttf",
        ROOT / "static" / "font" / "ResourceHanRoundedCN-Bold.ttf",
    ]
    font_path = next((p for p in cand_fonts if p.is_file()), None)

    pkg = types.ModuleType("_lb_render_pkg")
    pkg.__path__ = []
    libpkg = types.ModuleType("_lb_render_pkg.libraries")
    libpkg.__path__ = [str(ROOT / "libraries")]
    sys.modules["_lb_render_pkg"] = pkg
    sys.modules["_lb_render_pkg.libraries"] = libpkg

    cfg = types.ModuleType("_lb_render_pkg.config")
    cfg.Path = Path
    cfg.static = static_dir
    cfg.maimaidir = pic_dir
    cfg.coverdir = cover_dir
    cfg.log = logging.getLogger("lb-render-test")
    cfg.footer_generated = lambda *a, **k: "QQ Group 123456 | Milk Test"
    # 字体路径：存在用真字体，不存在给一个不存在的路径让加载器回退到 default
    fake_font = str(font_path) if font_path else str(font_dir / "missing.ttf")
    cfg.SIYUAN = Path(fake_font)
    cfg.TBFONT = Path(fake_font)
    cfg.SHANGGUMONO = Path(fake_font)
    sys.modules["_lb_render_pkg.config"] = cfg

    def _load(name: str, rel: str):
        spec = importlib.util.spec_from_file_location(
            "_lb_render_pkg.libraries." + name, str(ROOT / rel)
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_lb_render_pkg.libraries." + name] = mod
        spec.loader.exec_module(mod)
        return mod

    image_mod = _load("image", "libraries/image.py")
    theme = _load("maimaidx_theme", "libraries/maimaidx_theme.py")
    assets = _load("maimaidx_game_assets", "libraries/maimaidx_game_assets.py")
    lb = _load("maimaidx_leaderboard_image", "libraries/maimaidx_leaderboard_image.py")
    rep = _load("maimaidx_report_image", "libraries/maimaidx_report_image.py")
    risk = _load("maimaidx_risk_image", "libraries/maimaidx_risk_image.py")
    plate = _load("maimaidx_plate_image", "libraries/maimaidx_plate_image.py")
    awmc = _load("maimaidx_awmc_image", "libraries/maimaidx_awmc_image.py")
    return image_mod, theme, assets, lb, rep, risk, plate, awmc, cfg


def _make_fake_sprites(pic_dir: Path):
    """在主题目录写入比例正确的假贴图，供加载/几何测试。"""
    theme_dir = pic_dir / "prism_plus"
    theme_dir.mkdir(parents=True, exist_ok=True)
    # 评级贴图：任意比例的有效 PNG
    for name in ("SSSp", "SSS", "SSp", "SS", "Sp", "S", "AAA", "AA", "A", "BBB", "BB", "B", "C", "D"):
        Image.new("RGBA", (120, 60), (255, 100, 200, 255)).save(theme_dir / f"UI_TTR_Rank_{name}.png")
    # Rating 等级条：必须是 664x130，与 rating_badge_width() 的基准比例一致
    for n in range(1, 12):
        Image.new("RGBA", (664, 130), (80, 120, 255, 255)).save(theme_dir / f"UI_CMN_DXRating_{n:02d}.png")
    # 数字贴图
    for d in range(10):
        Image.new("RGBA", (17, 20), (255, 255, 255, 255)).save(pic_dir / f"UI_NUM_Drating_{d}.png")


def _clear_asset_caches(assets):
    for name in ("_load_rank_sprite", "_load_rating_bar", "_load_drating_digit"):
        fn = getattr(assets, name, None)
        if fn is not None:
            fn.cache_clear()
    assets.bold_font.cache_clear()
    assets.num_font.cache_clear()


# ----------------------------------------------------------------------
# 测试数据
# ----------------------------------------------------------------------
def _rating_rows(n=25):
    names = ["ARKKKKKK", "NIUNIU", "Losoy", "QMZBDX", "BAKA", "Redapple",
             "Yota!", "测试用户名字很长很长的那种", "Player9", "Player10",
             "Player11", "Player12", "Player13", "Player14", "Player15",
             "Player16", "Player17", "Player18", "Player19", "Player20",
             "Player21", "Player22", "Player23", "Player24", "Player25"]
    rows = []
    for i in range(n):
        ra = 16017 - i * 173
        rows.append((1000 + i, names[i] if i < len(names) else f"P{i}", max(ra, 1000)))
    return rows


def _song_rows(n=5):
    rates = ["sssp", "sssp", "ssp", "s", "aa"]
    rows = []
    for i in range(n):
        info = {
            "achievements": 100.7 - i * 1.2,
            "fc": "ap" if i == 0 else ("fc" if i == 1 else ""),
            "fs": "fdx",
            "dxScore": 2547 - i * 12,
            "rate": rates[i],
        }
        rows.append((1000 + i, f"Player{i+1}", info))
    return rows


def _gain_rows(n=6):
    return [(1000 + i, f"Player{i+1}", 15900 - i * 80, 16017 - i * 80, 117 - i * 18)
            for i in range(n)]


def _sun_lock_rows(n=5):
    return [(1000 + i, f"Player{i+1}", 3 - i % 3, 2 - i % 2) for i in range(n)]


def _gain_sections():
    return {
        "稳赚": [
            {"song_id": 1000, "title": "Oshama Scramble!", "level": "13+", "ds": 13.6,
             "fit_diff": 13.2, "achv_now": 99.8, "achv_target": 100.0, "need": 0.2,
             "net_gain": 18, "probability": 0.72},
            {"song_id": 1001, "title": "BREaK! BREaK!", "level": "13", "ds": 13.3,
             "fit_diff": 13.0, "achv_now": 99.6, "achv_target": 100.0, "need": 0.4,
             "net_gain": 15, "probability": 0.66},
        ],
        "均衡": [
            {"song_id": 1002, "title": "PANTS", "level": "13+", "ds": 13.8,
             "fit_diff": 13.5, "achv_now": 99.4, "achv_target": 99.5, "need": 0.1,
             "net_gain": 12, "probability": 0.5},
        ],
        "冲刺": [
            {"song_id": 1003, "title": "Caliburne", "level": "14", "ds": 14.2,
             "fit_diff": 14.5, "achv_now": 98.5, "achv_target": 99.0, "need": 0.5,
             "net_gain": 35, "probability": 0.25},
        ],
    }


def _report_data():
    class E:
        def __init__(self, t, l, li, ds, ra, ad, an, sid=1000):
            self.title = t; self.level = l; self.level_index = li; self.ds = ds
            self.ra_delta = ra; self.achv_delta = ad; self.achv_now = an; self.song_id = sid

    class R:
        def __init__(self, sid, t):
            self.song_id = sid; self.title = t; self.level = "13+"; self.level_index = 3
            self.ds = 13.6; self.achievements = 99.8; self.rate = "sss"; self.ra = 280

    best = E("Oshama Scramble!", "13+", 3, 13.6, 18, 0.2, 99.8, sid=1000)
    return {
        "rating_delta": 117, "old_rating": 15900, "new_rating": 16017,
        "b35_delta": 40, "b15_delta": 77, "b35_new_sum": 9000, "b15_new_sum": 4000,
        "b35_tail_delta": 3, "b15_tail_delta": 5,
        "new_entries": [R(2000 + i, f"新曲{i}") for i in range(4)],
        "improved": [best, E("PANTS", "14", 3, 14.0, 15, 0.1, 99.5, 1001),
                     E("Caliburne", "14", 3, 14.2, 12, 0.3, 98.7, 1002)],
        "improved_count": 12, "new_count": 3, "total_improved_ra": 86,
        "best_entry": best, "diff_dist": [0, 1, 4, 6, 1],
        "sun_list": [best], "lock_list": [E("Caliburne", "14", 3, 14.2, 8, 0.05, 99.9, 1002)],
        "new_b50": [R(1000 + i, f"旧曲{i}") for i in range(35)] + [R(2000 + i, f"新曲{i}") for i in range(15)],
    }



def _risk_items():
    return [
        {"title": "Oshama Scramble!", "level": "13+", "level_index": 3, "song_id": 1000,
         "ra": 280, "achv": 100.4999, "zone": "B35", "score": 62,
         "reasons": ["地板位", "寸止", "较上次-3ra"]},
        {"title": "PANTS GURG GURG GURG", "level": "14", "level_index": 3, "song_id": 1002,
         "ra": 295, "achv": 100.9900, "zone": "B15", "score": 30,
         "reasons": ["近地板(差2)", "锁血"]},
        {"title": "Caliburne", "level": "14", "level_index": 4, "song_id": 1003,
         "ra": 300, "achv": 101.1234, "zone": "B15", "score": 14,
         "reasons": ["近地板(差7)"]},
        {"title": "BREaK! BREaK!", "level": "13", "level_index": 2, "song_id": 1001,
         "ra": 270, "achv": 99.8000, "zone": "B35", "score": 8,
         "reasons": ["较早期-3ra"]},
    ]

def _plate_diffs():
    colors = [(72, 196, 120, 255), (245, 186, 60, 255), (240, 110, 130, 255),
              (156, 96, 220, 255)]
    return [
        {"name": n, "remaining": r, "total": t, "color": c}
        for n, r, t, c in zip(
            ["Basic", "Advanced", "Expert", "Master"],
            [27, 27, 26, 24], [80, 90, 95, 88], colors)
    ]


def _plate_songs():
    return [
        {"song_id": 365, "title": "ガラテアの螺旋", "level": "14.6",
         "level_index": 3, "ds": 14.6, "record": "", "played": False},
        {"song_id": 348, "title": "Axeria", "level": "14.5",
         "level_index": 3, "ds": 14.5, "record": "99.5000%", "played": True},
        {"song_id": 439, "title": "最終鬼畜妹・一部声", "level": "13.9",
         "level_index": 3, "ds": 13.9, "record": "FC", "played": True},
        {"song_id": 288, "title": "六兆年と一夜物語", "level": "13.8",
         "level_index": 3, "ds": 13.8, "record": "", "played": False},
    ]


def _awmc_profile():
    return {
        "qqid": 10001, "balance": 348, "streak": 12,
        "last_checkin_date": "2026-08-09", "checked_in_today": True,
        "free_used_today": False, "account_bound": True, "storage_enabled": True,
        "data_source": "divingfish", "theme": "prism_plus",
        "today_query_count": 7, "today_analysis_count": 2,
        "today_break_spent": 12, "today_break_gained": 5,
        "account_today_total": 4, "account_today_success": 4,
        "account_today_error": 0, "total_query_count": 1287,
        "total_analysis_count": 156, "account_total": 203,
        "account_total_success": 200, "account_total_error": 3,
        "last_query_at": 1754726400, "last_analysis_at": 1754720000,
        "account_operation_counts": {"bind": 1, "upload": 180, "ticket": 20},
        "recent_account_logs": [
            {"created_at": 1754726400, "operation": "upload",
             "status": "success", "ref_id": "ref-001"},
            {"created_at": 1754720000, "operation": "ticket",
             "status": "error", "ref_id": "ref-002"},
        ],
        "recent_logs": [
            {"delta": 5, "reason": "checkin", "created_at": 1754726400},
            {"delta": -7, "reason": "query", "created_at": 1754720000},
            {"delta": 2, "reason": "guess_reward", "created_at": 1754710000},
        ],
    }



# ----------------------------------------------------------------------
# 断言工具
# ----------------------------------------------------------------------
def _assert_png(bio: bytes, label: str, *, min_h: int = 200, max_h: int = 4000):
    im = Image.open(bio)
    im.load()
    assert im.format == "PNG", f"{label}: 输出不是 PNG"
    assert im.width == WIDTH, f"{label}: 宽度 {im.width} != {WIDTH}"
    assert min_h <= im.height <= max_h, f"{label}: 高度 {im.height} 超出合理范围 [{min_h},{max_h}]"
    return im


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if sys.version_info < (3, 10) else asyncio.run(coro)


# ----------------------------------------------------------------------
# 各测试
# ----------------------------------------------------------------------
def test_smoke_no_assets(mods):
    _, _, assets, lb, rep, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)

    all_rows = _rating_rows(25)
    bio = _run(lb.render_rating_ranking(all_rows[:8], title="群 Rating 排行",
                                        subtitle="共 25 人 · 显示前 8 名",
                                        self_qq=1002, self_rank=3, all_rows=all_rows,
                                        user_name="Losoy"))
    im = _assert_png(bio, "群Rating榜", min_h=800)
    print(f"  群Rating榜 {im.size} OK（全群25人统计）")

    bio = _run(lb.render_song_leaderboard(
        _song_rows(5), "Restricted Access", "Master", level_index=3,
        total_players=5, self_qq=1001, user_name="Losoy"))
    im = _assert_png(bio, "单曲榜", min_h=800)
    print(f"  单曲榜 {im.size} OK")

    bio = _run(lb.render_gain_ranking(_gain_rows(6), "群吃分榜", "近7天",
                                      self_qq=1002, user_name="Losoy"))
    im = _assert_png(bio, "吃分榜", min_h=600)
    print(f"  吃分榜 {im.size} OK")

    bio = _run(lb.render_sun_lock_ranking(_sun_lock_rows(5), "寸止榜", "近7天",
                                          mode="sun", user_name="Losoy"))
    im = _assert_png(bio, "寸止榜", min_h=600)
    print(f"  寸止榜 {im.size} OK")

    trend = [("07-27", 15820), ("07-30", 15860), ("08-01", 15900),
             ("08-04", 15930), ("08-06", 15980), ("08-09", 16017)]
    bio = lb.render_gain_recommendation(_gain_sections(),
                                        ["昨日存档 2026-08-08", "能力样本 14 天", "候选 36 首"],
                                        user_name="Losoy",
                                        rating_trend=trend, current_rating=16017)
    im = _assert_png(bio, "吃分推荐", min_h=400)
    print(f"  吃分推荐(含趋势) {im.size} OK")
    # 无趋势/无当前 rating 也不能崩
    bio = lb.render_gain_recommendation(_gain_sections(), ["摘要"], user_name="Losoy")
    _assert_png(bio, "吃分推荐无趋势", min_h=400)

    data = _report_data()
    for tag, pts, labs, min_h in [
        ("日报", [15900, 16017], ["08-08", "08-09"], 1200),
        ("周报", [15900, 15940, 15980, 16000, 15990, 16017], [f"D{i}" for i in range(6)], 1200),
    ]:
        bio = rep.render_report(f"MAIMAI {tag}", "Losoy", "2026-08 → 2026-08",
                                pts, labs, data, period_tag=tag)
        im = _assert_png(bio, f"{tag}", min_h=min_h)
        print(f"  {tag} {im.size} OK")


def test_sprite_geometry(mods):
    """有假贴图时：评级贴图可加载、Rating 徽章宽度契约一致、不越界错位。"""
    _, _, assets, lb, _, _, _, _, cfg = mods
    _make_fake_sprites(cfg.maimaidir)
    _clear_asset_caches(assets)

    # 评级贴图加载
    for rate in ("sssp", "sss", "ssp", "ss", "sp", "s", "aaa", "aa", "a", "bbb", "bb", "b", "c", "d"):
        assert assets.has_rank_sprite(rate), f"评级贴图 {rate} 未被加载"
    canvas = Image.new("RGBA", (400, 100), (0, 0, 0, 0))
    w, h = assets.draw_rank_sprite(canvas, 10, 20, height=40, rate_key="sssp")
    assert h == 40 and w > 0, f"评级贴图几何异常 w={w} h={h}"
    print(f"  评级贴图加载与等比缩放 OK（{w}x{h}）")

    # Rating 徽章：draw 返回宽度必须等于 rating_badge_width()，否则右对齐会错位
    for height in (30, 34, 40):
        canvas = Image.new("RGBA", (800, 120), (0, 0, 0, 0))
        right_x = 760
        bw, bh = assets.draw_rating_badge(canvas, right_x - assets.rating_badge_width(height),
                                          40, 16017, height=height)
        expected_w = assets.rating_badge_width(height)
        assert bw == expected_w, (
            f"Rating 徽章宽度错位 h={height}: draw 返回 {bw}, rating_badge_width={expected_w}；"
            f"UI_CMN_DXRating 贴图比例必须为 664x130")
        # 徽章右边缘不应越过 right_x
        assert right_x - assets.rating_badge_width(height) + bw <= right_x + 1
    print("  Rating 徽章宽度契约一致（无错位）OK")

    # 排行榜自身的右对齐封装
    canvas = Image.new("RGBA", (WIDTH, 120), (0, 0, 0, 0))
    bw, bh = lb._draw_rating_badge(canvas, WIDTH - 40, 60, 16017, height=34)
    assert bw > 0 and bh == 34
    print(f"  排行榜徽章封装 OK（{bw}x{bh}）")

    # 用假贴图完整渲染一张 Rating 榜，确认不抛异常
    all_rows = _rating_rows(12)
    bio = _run(lb.render_rating_ranking(all_rows[:10], all_rows=all_rows))
    _assert_png(bio, "带贴图Rating榜", min_h=800)
    print("  带贴图 Rating 榜整图渲染 OK")


def test_fallback_when_assets_missing(mods):
    """无贴图时：draw_* 返回 (0,0)，调用方回退，不抛异常。"""
    _, _, assets, lb, _, _, _, _, cfg = mods
    # 清空主题目录
    import shutil
    for sub in cfg.maimaidir.glob("prism_plus"):
        shutil.rmtree(sub, ignore_errors=True)
    for p in cfg.maimaidir.glob("UI_NUM_Drating_*.png"):
        p.unlink(missing_ok=True)
    _clear_asset_caches(assets)

    assert not assets.has_rank_sprite("sss")
    canvas = Image.new("RGBA", (200, 80), (0, 0, 0, 0))
    assert assets.draw_rank_sprite(canvas, 0, 0, height=40, rate_key="sss") == (0, 0)
    assert assets.draw_rating_badge(canvas, 0, 0, 16017, height=34) == (0, 0)
    # 排行榜封装应走到彩色胶囊回退，仍返回正宽度
    bw, bh = lb._draw_rating_badge(canvas, WIDTH - 40, 40, 16017, height=34)
    assert bw > 0 and bh == 34
    print("  无素材回退（评级/Rating 胶囊）OK")


def test_square_cover_and_no_paw(mods):
    """曲绘占位图为正方形；源码不再含爪印绘制。"""
    _, _, _, lb, _, _, _, _, _ = mods
    for size in (48, 68, 72, 88):
        im = lb._cover_placeholder(size)
        assert im.size == (size, size), f"曲绘占位不是方形: {im.size}"
    print("  曲绘占位图方形 OK")

    src = (ROOT / "libraries" / "maimaidx_leaderboard_image.py").read_text(encoding="utf-8")
    assert "_draw_paw" not in src, "仍存在爪印绘制函数 _draw_paw"
    assert "paw" not in src.lower(), "源码仍引用 paw（爪印）"
    print("  爪印已移除 OK")

    # 单曲榜顶部曲绘必须走方形 alpha_composite（不是圆形/圆角）
    assert "im.alpha_composite(cover, (mx + 16, hcard_y + 11))" in src
    print("  单曲榜顶部曲绘方形粘贴 OK")


def test_utage_filter_source():
    """报告 B50 构建必须过滤宴会场谱面（song_id >= 100000）。"""
    src = (ROOT / "libraries" / "maimaidx_progress_report.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_b50":
            body = ast.get_source_segment(src, node)
            assert "100000" in body, "_build_b50 中未发现宴谱过滤阈值 100000"
            assert "< 100000" in body, "_build_b50 未使用 '< 100000' 过滤宴谱"
            found = True
    assert found, "未找到 _build_b50 函数"
    print("  报告 B50 宴谱过滤（song_id<100000）OK")

    # 吃分推荐必须调用 filter_utage_records
    rec_src = (ROOT / "libraries" / "maimaidx_gain_recommend.py").read_text(encoding="utf-8")
    assert "filter_utage_records" in rec_src, "吃分推荐未过滤宴谱"
    print("  吃分推荐宴谱过滤 OK")


def test_all_rows_decouples_stats(mods):
    """all_rows 行数与显示行数解耦：显示 3 人但统计全群 25 人也能出图。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)
    all_rows = _rating_rows(25)
    bio = _run(lb.render_rating_ranking(all_rows[:3], all_rows=all_rows))
    im = _assert_png(bio, "全群统计", min_h=800)
    # 全群人数 25 的统计面板应让图比只显示 3 人无统计时更高
    bio_small = _run(lb.render_rating_ranking(all_rows[:3], all_rows=all_rows[:3]))
    im_small = _assert_png(bio_small, "小样本", min_h=400)
    assert im.height >= im_small.height
    print(f"  全群统计解耦 OK（25人图高 {im.height} >= 3人图高 {im_small.height}）")



def test_no_chinese_in_mono_font():
    """静态扫描：_font_mono（西文/数字字体）不得渲染含中文/全角符号的字面量，防止豆腐块。"""
    cjk_ranges = [
        (0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF),
    ]

    def has_cjk(text):
        return any(any(lo <= ord(ch) <= hi for lo, hi in cjk_ranges) for ch in text)

    def literal_text(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value if has_cjk(node.value) else None
        if isinstance(node, ast.JoinedStr):
            chars = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    chars.append(v.value)
            joined = "".join(chars)
            return joined if has_cjk(joined) else None
        return None

    bad = []
    for rel in ("libraries/maimaidx_leaderboard_image.py",
                "libraries/maimaidx_report_image.py",
                "libraries/maimaidx_risk_image.py",
                "libraries/maimaidx_plate_image.py",
                "libraries/maimaidx_awmc_image.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "text"):
                continue
            font_arg = None
            for kw in node.keywords:
                if kw.arg == "font":
                    font_arg = kw.value
            if not (isinstance(font_arg, ast.Call) and isinstance(font_arg.func, ast.Name)
                    and font_arg.func.id == "_font_mono"):
                continue
            text_arg = node.args[1] if len(node.args) >= 2 else None
            hit = literal_text(text_arg) if text_arg is not None else None
            if hit:
                bad.append(f"{rel}:{node.lineno} 用 _font_mono 渲染含中文文本: {hit!r}")
    assert not bad, "发现西文字体渲染中文（会显示豆腐块）：\n  " + "\n  ".join(bad)
    print("  无 _font_mono 渲染中文（豆腐块防护）OK")


def test_render_without_font_files(mods):
    """字体文件全部缺失时，加载器回退 PIL 默认字体，渲染不应抛异常。"""
    _, _, assets, lb, rep, risk, plate, awmc, _cfg = mods
    # 只把内存中的字体路径指向不存在的文件（绝不删除磁盘上的真实字体）
    assets.SIYUAN = Path("/nonexistent/missing-bold.ttf")
    assets.TBFONT = Path("/nonexistent/missing-num.ttf")
    _clear_asset_caches(assets)

    # 不能崩溃；豆腐由 PIL 默认字体兜底（生产环境字体齐全，不会走到这里）
    all_rows = _rating_rows(8)
    bio = _run(lb.render_rating_ranking(all_rows[:5], all_rows=all_rows))
    _assert_png(bio, "无字体-Rating榜", min_h=600)
    bio = lb.render_gain_recommendation(_gain_sections(), ["摘要一", "能力样本"])
    _assert_png(bio, "无字体-吃分推荐", min_h=300)
    bio = rep.render_report("MAIMAI 日报", "Losoy", "2026-08-08 → 2026-08-09",
                            [15900, 16017], ["08-08", "08-09"], _report_data(),
                            period_tag="日报")
    _assert_png(bio, "无字体-日报", min_h=1000)
    print("  无字体文件回退不崩溃 OK")


def test_my_rank_context(mods):
    """我在群里有多菜：以用户为中心的前后排名上下文，覆盖中位/榜首/榜尾/未找到。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)
    names = [f"Player{i}" for i in range(15)]
    rows = [(1000 + i, names[i], 16017 - i * 120) for i in range(15)]

    # 中位：显示前后各 5 名，自己高亮，真实排名
    bio = _run(lb.render_my_rank_context(rows, self_qq=1002, half=5))
    im = _assert_png(bio, "我的排名-中位", min_h=800)
    print(f"  我的排名(中位 rank3) {im.size} OK")

    # 榜首
    bio = _run(lb.render_my_rank_context(rows, self_qq=1000, half=5))
    _assert_png(bio, "我的排名-榜首", min_h=800)
    # 榜尾
    bio = _run(lb.render_my_rank_context(rows, self_qq=1014, half=5))
    _assert_png(bio, "我的排名-榜尾", min_h=800)
    # 未找到返回 None（调用方给文字提示）
    assert _run(lb.render_my_rank_context(rows, self_qq=99999, half=5)) is None
    print("  榜首/榜尾/未找到边界 OK")


def _b1b36_ref():
    return {
        "b1": {"song_id": 1000, "title": "Oshama Scramble!", "level": "14",
               "level_index": 3, "ds": 14.0, "ra": 305, "achievements": 100.85},
        "b36": {"song_id": 1003, "title": "Caliburne", "level": "14",
                "level_index": 4, "ds": 14.2, "ra": 300, "achievements": 101.1234},
    }


def test_board_b1b36_and_overflow(mods):
    """群榜/我的排名带 B1/B36 参照；底部统计面板不溢出（最后一行在画布内）。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)
    ref = _b1b36_ref()

    all_rows = _rating_rows(145)
    bio = _run(lb.render_rating_ranking(
        all_rows[:10], all_rows=all_rows, self_qq=1001, self_rank=2,
        user_name="MILKA...", b1b36=ref))
    im = _assert_png(bio, "群榜-B1B36", min_h=1000)
    w, h = im.size
    # 底部统计面板最后一行（<12000 图例）必须在白卡内、且未越过 footer
    px = im.convert("RGBA").load()
    bottom_band_ok = False
    for yy in range(h - 110, h - 70):
        if px[w // 2, yy][:3] != (0, 0, 0):
            bottom_band_ok = True
    assert bottom_band_ok, "群榜底部统计区域疑似溢出/黑边"
    print(f"  群榜(B1/B36, 145人) {im.size} OK，底部无溢出")

    names = [f"Player{i}" for i in range(15)]
    rows = [(1000 + i, names[i], 16017 - i * 120) for i in range(15)]
    bio = _run(lb.render_my_rank_context(
        rows, self_qq=1002, half=5, user_name="MILKA...", b1b36=ref))
    im = _assert_png(bio, "我的排名-B1B36", min_h=900)
    print(f"  我的排名(B1/B36) {im.size} OK")

    # 每个窗口用户携带各自 B1/B36：应正常出图，且行高已为两行榜首信息预留
    row_b1b36 = {}
    for i in range(15):
        row_b1b36[1000 + i] = {
            "b1": {"song_id": 1000 + i, "title": f"SD Song {i}", "level": "13+",
                   "level_index": 3, "ra": 305 - i, "achievements": 100.5},
            "b36": {"song_id": 2000 + i, "title": f"DX Song {i}", "level": "14",
                    "level_index": 4, "ra": 300 - i, "achievements": 100.7},
        }
    bio = _run(lb.render_my_rank_context(
        rows, self_qq=1002, half=5, user_name="MILKA...",
        b1b36=row_b1b36[1002], row_b1b36=row_b1b36))
    im = _assert_png(bio, "我的排名-逐行B1B36", min_h=900)
    print(f"  我的排名(逐行 B1/B36) {im.size} OK")

    # 无 b1/b36（仅有其一或全空）不应崩
    bio = _run(lb.render_rating_ranking(
        all_rows[:10], all_rows=all_rows,
        b1b36={"b1": None, "b36": ref["b36"]}))
    _assert_png(bio, "群榜-部分B36", min_h=900)
    print("  部分/空 B1/B36 回退 OK")



def test_canvas_fully_painted(mods):
    """渲染图四角必须完全不透明且非纯黑，防止透明边/黑边造成的错位观感。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)
    bio = _run(lb.render_rating_ranking(_rating_rows(6)[:6], all_rows=_rating_rows(6)))
    im = Image.open(bio).convert("RGBA")
    w, h = im.size
    corners = [im.getpixel((0, 0)), im.getpixel((w - 1, 0)),
               im.getpixel((0, h - 1)), im.getpixel((w - 1, h - 1))]
    for px in corners:
        assert px[3] == 255, f"角落存在透明像素 {px}，背景未填满"
        assert not (px[0] == 0 and px[1] == 0 and px[2] == 0), f"角落出现纯黑 {px}"
    print(f"  画布背景填满无透明/黑边 OK（四角 {corners[0]}）")



def test_b50_risk_report(mods):
    """B50 风险新风格渲染：有风险/无风险均正常出图，宽度 1080，四角不透明。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)

    items = _risk_items()
    bio = risk.render_risk_report("Losoy", 14, items, b50_total=50, user_name="Losoy")
    im = _assert_png(bio, "B50风险-有数据", min_h=600)
    print(f"  B50风险(有数据) {im.size} OK")

    # 无风险曲目：应渲染空状态卡，不崩
    bio = risk.render_risk_report("Losoy", 14, [], b50_total=50, user_name="Losoy")
    im = _assert_png(bio, "B50风险-空状态", min_h=400)
    print(f"  B50风险(空状态) {im.size} OK")

    # 字体缺失回退
    assets.SIYUAN = Path("/nonexistent/missing-bold.ttf")
    assets.TBFONT = Path("/nonexistent/missing-num.ttf")
    _clear_asset_caches(assets)
    bio = risk.render_risk_report("Losoy", 14, items, b50_total=50, user_name="Losoy")
    _assert_png(bio, "B50风险-无字体", min_h=400)
    print("  B50风险 无字体回退 OK")



def test_plate_progress(mods):
    """牌子进度新风格：有列表/空列表(完成)/notice 三态正常出图，宽度 1080。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)

    diffs = _plate_diffs()
    bio = plate.render_plate_progress(
        plate_title="暁将", goal="达成率 ≥100%", diffs=diffs,
        songs=_plate_songs(), list_title="剩余定数大于 13.6 的曲目",
        user_name="Losoy")
    im = _assert_png(bio, "牌子进度-列表", min_h=600)
    print(f"  牌子进度(列表) {im.size} OK")

    done = [{**d, "remaining": 0} for d in diffs]
    bio = plate.render_plate_progress(
        plate_title="暁将", goal="达成率 ≥100%", diffs=done,
        completed=True, user_name="Losoy")
    im = _assert_png(bio, "牌子进度-完成", min_h=400)
    print(f"  牌子进度(完成) {im.size} OK")

    bio = plate.render_plate_progress(
        plate_title="暁将", goal="达成率 ≥100%", diffs=diffs,
        notice="还有 68 首定数大于 13.6 的曲目，加油推分捏！", user_name="Losoy")
    _assert_png(bio, "牌子进度-提示", min_h=400)
    print("  牌子进度(notice) OK")


def test_awmc_profile(mods):
    """我的 AWMC 新风格：含余额/统计/偏好/记录正常出图，缺记录也不崩。"""
    _, _, assets, lb, _, risk, plate, awmc, _ = mods
    _clear_asset_caches(assets)

    bio = awmc.render_awmc_profile(_awmc_profile(), user_name="Losoy")
    im = _assert_png(bio, "AWMC-完整", min_h=600)
    print(f"  我的AWMC(完整) {im.size} OK")

    minimal = {"qqid": 42, "balance": 0}
    bio = awmc.render_awmc_profile(minimal, user_name="Milk")
    im = _assert_png(bio, "AWMC-最小", min_h=400)
    print(f"  我的AWMC(最小) {im.size} OK")

    # 字体缺失回退
    assets.SIYUAN = Path("/nonexistent/missing-bold.ttf")
    assets.TBFONT = Path("/nonexistent/missing-num.ttf")
    _clear_asset_caches(assets)
    bio = plate.render_plate_progress(
        plate_title="暁将", goal="达成率 ≥100%", diffs=_plate_diffs(),
        songs=_plate_songs(), list_title="剩余", user_name="Losoy")
    _assert_png(bio, "牌子-无字体", min_h=400)
    bio = awmc.render_awmc_profile(_awmc_profile(), user_name="Losoy")
    _assert_png(bio, "AWMC-无字体", min_h=400)
    print("  牌子/AWMC 无字体回退 OK")


def main():
    assert sys.version_info >= (3, 9), "需要 Python 3.9+"
    with TemporaryDirectory() as tmp:
        static_dir = Path(tmp)
        mods = _load_render_modules(static_dir)

        print("[1/14] 无素材冒烟渲染")
        test_smoke_no_assets(mods)

        print("[2/14] 贴图加载与几何/错位校验")
        test_sprite_geometry(mods)

        print("[3/14] 无素材回退")
        test_fallback_when_assets_missing(mods)

        print("[4/14] 方形曲绘 / 去爪印源码校验")
        test_square_cover_and_no_paw(mods)

        print("[5/14] 宴谱过滤源码校验")
        test_utage_filter_source()

        print("[6/14] 全群统计解耦")
        test_all_rows_decouples_stats(mods)

        print("[7/14] 西文字体渲染中文静态扫描")
        test_no_chinese_in_mono_font()

        print("[8/14] 无字体文件回退")
        test_render_without_font_files(mods)

        print("[9/14] 画布背景完整性")
        test_canvas_fully_painted(mods)

        print("[10/14] 我的排名上下文")
        test_my_rank_context(mods)

        print("[11/14] B50 风险新风格")
        test_b50_risk_report(mods)

        print("[12/14] 牌子进度新风格")
        test_plate_progress(mods)

        print("[13/14] 我的 AWMC 新风格")
        test_awmc_profile(mods)

        print("[14/14] 群榜 B1/B36 与底部溢出")
        test_board_b1b36_and_overflow(mods)

    print("\nALL RENDER TESTS PASSED 🐾")


if __name__ == "__main__":
    main()
