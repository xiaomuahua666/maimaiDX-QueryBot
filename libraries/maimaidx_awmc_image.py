"""
「我的 AWMC」账号卡片的现代化图片渲染。

统一明亮卡片风格，展示 BREAK 余额、签到、今日 / 累计使用、偏好与最近记录。
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional

from PIL import ImageDraw

from .maimaidx_leaderboard_image import (
    _ACCENT, _CARD_BORDER, _GOLD, _GREEN, _MUTED, _RED, _TEXT, _TEXT_SOFT,
    _bar, _brand_mark, _card, _finalize, _font_bold, _font_mono, _footer,
    _make_bg, _period_chip, _text_len, _truncate,
)

_WIDTH = 1080
_MX = 40

_OPERATION_COLORS = (
    (74, 144, 217, 255),
    (230, 140, 70, 255),
    (72, 180, 120, 255),
    (200, 120, 200, 255),
    (240, 190, 80, 255),
    (235, 88, 112, 255),
    (60, 180, 170, 255),
    (132, 112, 244, 255),
)


def _ts(val) -> str:
    if not val:
        return '暂无'
    try:
        return datetime.fromtimestamp(float(val)).strftime('%m-%d %H:%M')
    except (TypeError, ValueError):
        return '暂无'



def _g(obj, key, default=None):
    """兼容 dict 与 pydantic/普通对象的字段读取。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stat_tile(im, d, x, y, w, h, value, label, color=_TEXT):
    _card(im, (x, y, x + w, y + h), radius=16,
          fill=(245, 247, 252, 255), shadow=False)
    d.text((x + w // 2, y + h // 2 - 8), str(value),
           font=_font_mono(26), fill=color, anchor='mm')
    d.text((x + w // 2, y + h // 2 + 18), str(label),
           font=_font_bold(13), fill=_TEXT_SOFT, anchor='mm')


def _section_title(d, x, y, text, color=_TEXT):
    d.ellipse((x, y + 6, x + 10, y + 16), fill=color)
    d.text((x + 18, y + 11), text, font=_font_bold(20), fill=_TEXT, anchor='lm')


def _kv_row(d, x, y, w, key, value, value_color=_TEXT):
    d.text((x, y), key, font=_font_bold(15), fill=_TEXT_SOFT)
    d.text((x + w, y), str(value), font=_font_bold(15),
           fill=value_color, anchor='rt')


def _operation_items(op_counts: Dict[str, int], labels: Dict[str, str]):
    items = [
        (str(name), labels.get(str(name), str(name)), max(0, int(count or 0)))
        for name, count in op_counts.items()
        if int(count or 0) > 0
    ]
    items.sort(key=lambda item: (-item[2], item[0]))
    if len(items) <= 8:
        return items
    other = sum(item[2] for item in items[7:])
    return items[:7] + [('other', '其他功能', other)]


def _draw_operation_distribution(im, d, x, y, w, items):
    total = sum(item[2] for item in items)
    cx, cy, radius = x + 142, y + 148, 88
    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
    if total <= 0:
        d.ellipse(bbox, outline=(225, 230, 242, 255), width=14)
    else:
        start = -90.0
        for index, (_, _, count) in enumerate(items):
            extent = 360.0 * count / total
            color = _OPERATION_COLORS[index % len(_OPERATION_COLORS)]
            if extent >= 359.999:
                d.ellipse(bbox, fill=color)
            else:
                d.pieslice(bbox, start=start, end=start + extent, fill=color)
            start += extent
    hole = 50
    d.ellipse((cx - hole, cy - hole, cx + hole, cy + hole), fill=(255, 255, 255, 255))
    d.text((cx, cy - 8), str(total), font=_font_mono(24), fill=_TEXT, anchor='mm')
    d.text((cx, cy + 18), '次调用', font=_font_bold(12), fill=_MUTED, anchor='mm')

    legend_x = x + 280
    legend_w = w - 304
    col_w = legend_w // 2
    for index, (_, label, count) in enumerate(items):
        row, col = divmod(index, 2)
        lx = legend_x + col * col_w
        ly = y + 76 + row * 48
        color = _OPERATION_COLORS[index % len(_OPERATION_COLORS)]
        pct = 100.0 * count / total if total else 0.0
        d.ellipse((lx, ly - 6, lx + 12, ly + 6), fill=color)
        font = _font_bold(14)
        text = _truncate(d, label, font, col_w - 118)
        d.text((lx + 20, ly), text, font=font, fill=_TEXT, anchor='lm')
        d.text((lx + col_w - 12, ly), f'{count} · {pct:.0f}%',
               font=_font_mono(13), fill=color, anchor='rm')
        _bar(im, lx + 20, ly + 14, col_w - 32, 6, count / total if total else 0, color)


def render_awmc_profile(profile: Dict,
                        *,
                        title: str = '我的 AWMC 账号',
                        user_name: str = 'Milk') -> BytesIO:
    width = _WIDTH
    mx = _MX
    inner_w = width - mx * 2

    qqid = profile.get('qqid', '-')
    balance = int(profile.get('balance', 0) or 0)
    streak = int(profile.get('streak', 0) or 0)
    last_checkin = profile.get('last_checkin_date') or '暂无'
    checked_in = bool(profile.get('checked_in_today'))
    free_used = bool(profile.get('free_used_today'))
    bound = bool(profile.get('account_bound'))
    storage = bool(profile.get('storage_enabled'))
    src = '落雪' if profile.get('data_source') == 'lxns' else '水鱼'
    theme = profile.get('theme') or 'default'

    today_query = int(profile.get('today_query_count', 0) or 0)
    today_analysis = int(profile.get('today_analysis_count', 0) or 0)
    today_spent = int(profile.get('today_break_spent', 0) or 0)
    today_gained = int(profile.get('today_break_gained', 0) or 0)
    acc_today_total = int(profile.get('account_today_total', 0) or 0)
    acc_today_ok = int(profile.get('account_today_success', 0) or 0)
    acc_today_err = int(profile.get('account_today_error', 0) or 0)

    total_query = int(profile.get('total_query_count', 0) or 0)
    total_analysis = int(profile.get('total_analysis_count', 0) or 0)
    acc_total = int(profile.get('account_total', 0) or 0)
    acc_ok = int(profile.get('account_total_success', 0) or 0)
    acc_err = int(profile.get('account_total_error', 0) or 0)
    last_query = _ts(profile.get('last_query_at'))
    last_analysis = _ts(profile.get('last_analysis_at'))

    op_counts: Dict[str, int] = profile.get('account_operation_counts') or {}
    ticket = profile.get('account_ticket_stats') or {}
    recent_acc: List[dict] = profile.get('recent_account_logs') or []
    recent_break = list(profile.get('recent_logs') or [])

    op_labels = {
        'bind': '账号绑定', 'claim': '账号认领', 'unbind': '账号解绑',
        'status': '账号状态', 'upload': '成绩上传',
        'upload_fish': '上传水鱼', 'upload_lx': '上传落雪',
        'upload_all': '同时上传', 'upload_awmcnet': 'AWMCNET同步',
        'ticket': '发票', 'ticket_status': '票券状态',
        'ticket_unused_penalty': '重复发票惩罚',
        'bind_fish': '绑定水鱼', 'bind_lx': '绑定落雪',
        'awmc_preview': '账号预览', 'awmc_items': '道具查询',
        'awmc_gate_status': '门状态',
        'awmc_music_upsert': '成绩编辑', 'awmc_music_delete': '成绩删除',
        'awmc_item_upsert': '道具修改', 'music_edit': '成绩编辑',
    }
    operation_items = _operation_items(op_counts, op_labels)
    ticket_total = int(ticket.get('total') or 0)
    reason_map = {
        'query': '查分', 'checkin': '签到', 'checkin_makeup': '补签',
        'checkin_storage_bonus': '签到·存储加成', 'today_luck': '今日舞萌',
        'b50_analysis': '分析b50', 'b50_analysis_precharge': '分析b50·预扣',
        'b50_analysis_refund': '分析b50·退款',
        'b50_analysis_settlement': '分析b50·结算',
        'busy_request_surcharge': '高负载附加费', 'guess_reward': '猜歌奖励',
        'admin_set': '管理员设置', 'admin_add': '管理员调整',
        'feishu_admin': '人工操作', 'web_admin': 'Web管理',
        'image_render': '图片渲染', 'search': '搜索',
        'gamble_all': '倾家荡产', 'gamble_pool_reward': '抽奖池奖励',
        'lottery': '抽奖', 'transfer_out': '转账转出', 'transfer_in': '转账收入',
        'card_redeem': '卡密兑换',
        'rating_guess_settlement': 'Rating猜歌',
        'b50_impostor_settlement': '冒牌者结算',
        'duel_all_clear_bonus': '对决全胜',
        'letter_settlement': '信件结算',
        'red_packet_create': '红包创建',
        'red_packet_claim': '红包领取',
        'red_packet_refund': '红包退款',
    }
    service_labels = {
        'upload': '成绩上传', 'ticket': '发票',
        'ticket_status': '票券状态查询',
        'awmc_status': '账号状态查询',
        'awmc_preview': '账号预览查询', 'awmc_items': '道具查询',
        'awmc_gate_status': '门状态查询',
        'awmc_music_upsert': '成绩编辑', 'awmc_music_delete': '成绩删除',
        'awmc_item_upsert': '道具修改',
        'upload_fish': '上传水鱼', 'upload_lx': '上传落雪',
        'upload_all': '同时上传', 'awmcnet_sync': 'AWMCNET同步',
        'coop_b50': '合作B50', 'today_gain_recommend': '今日推荐',
        'weekly_report': '周报', 'monthly_report': '月报',
        'annual_report': '年报', 'daily_report': '日报',
    }
    once_reward_labels = {
        'forum_bind_welcome': '论坛绑定欢迎',
    }

    # ---- 高度估算 ----
    hero_h = 156
    y = 118 + hero_h + 18

    # 今日使用 + 累计统计两张卡
    today_h = 150
    y += today_h + 16
    total_h = 150
    y += total_h + 16

    # 偏好卡
    pref_h = 56
    y += pref_h + 16

    # 功能分布 / 发票
    distribution_h = 0
    if operation_items:
        distribution_h = 292
    if ticket_total:
        distribution_h += 92
    if distribution_h:
        y += distribution_h + 16

    # 最近记录
    log_lines = min(5, len(recent_acc)) + min(20, len(recent_break))
    if log_lines:
        y += 40 + log_lines * 26 + 16
    y += 20
    canvas_h = y + 80

    im = _make_bg(width, canvas_h)
    d = ImageDraw.Draw(im)
    _brand_mark(im, width, user_name)
    _period_chip(im, width, 'AWMC')

    d.text((mx + 200, 44), title, font=_font_bold(32), fill=_TEXT)
    d.text((mx + 200, 86), f'QQ {qqid}  ·  数据源 {src}',
           font=_font_bold(17), fill=_TEXT_SOFT)

    # ---- Hero ----
    _card(im, (mx, 118, mx + inner_w, 118 + hero_h), radius=24,
          fill=(255, 255, 255, 230))
    d.text((mx + 30, 118 + 24), 'BREAK 余额',
           font=_font_bold(16), fill=_MUTED)
    d.text((mx + 30, 118 + 46), f'{balance}',
           font=_font_mono(46), fill=_GOLD)
    d.text((mx + 30, 118 + 108),
           f'连续签到 {streak} 天  ·  上次 {last_checkin}',
           font=_font_bold(15), fill=_TEXT_SOFT)

    # 状态 chips
    chips = [
        ('今日签到', '已完成' if checked_in else '未签到', _GREEN if checked_in else _MUTED),
        ('免费查分', '已用' if free_used else '可用', _MUTED if free_used else _GREEN),
        ('舞萌账号', '已绑定' if bound else '未绑定', _GREEN if bound else _RED),
        ('数据存储', '已开启' if storage else '未开启', _GREEN if storage else _MUTED),
    ]
    chip_x = mx + 300
    chip_w = (inner_w - 330) // 2 - 8
    for i, (label, val, col) in enumerate(chips):
        cx = chip_x + (i % 2) * (chip_w + 16)
        cy = 118 + 24 + (i // 2) * 62
        _card(im, (cx, cy, cx + chip_w, cy + 50), radius=14,
              fill=(245, 247, 252, 255), shadow=False)
        d.text((cx + 16, cy + 16), label, font=_font_bold(13), fill=_MUTED)
        d.text((cx + chip_w - 16, cy + 28), val, font=_font_bold(16),
               fill=col, anchor='rm')

    # ---- 今日使用 ----
    y = 118 + hero_h + 18
    _card(im, (mx, y, mx + inner_w, y + today_h), radius=20,
          fill=(255, 255, 255, 225))
    _section_title(d, mx + 22, y + 16, '今日使用', _ACCENT)
    tile_w = (inner_w - 44 - 3 * 12) // 4
    tx = mx + 22
    ty = y + 52
    _stat_tile(im, d, tx, ty, tile_w, 60, today_query, '查分 API', _TEXT)
    _stat_tile(im, d, tx + (tile_w + 12), ty, tile_w, 60, today_analysis, '分析 b50', _TEXT)
    _stat_tile(im, d, tx + 2 * (tile_w + 12), ty, tile_w, 60, today_spent, 'BREAK 消耗', _RED)
    _stat_tile(im, d, tx + 3 * (tile_w + 12), ty, tile_w, 60, today_gained, 'BREAK 获得', _GREEN)
    acc_rate = f'{acc_today_ok}/{acc_today_total}' if acc_today_total else '0/0'
    _kv_row(d, mx + 22, y + today_h - 30, inner_w - 44,
            f'账号功能  成功 {acc_today_ok} / 失败 {acc_today_err}',
            f'合计 {acc_rate}', _TEXT_SOFT)
    y += today_h + 16

    # ---- 累计统计 ----
    _card(im, (mx, y, mx + inner_w, y + total_h), radius=20,
          fill=(255, 255, 255, 225))
    _section_title(d, mx + 22, y + 16, '累计统计', _ACCENT)
    _stat_tile(im, d, tx, y + 52, tile_w, 60, total_query, '查分 API', _TEXT)
    _stat_tile(im, d, tx + (tile_w + 12), y + 52, tile_w, 60, total_analysis, '分析 b50', _TEXT)
    _stat_tile(im, d, tx + 2 * (tile_w + 12), y + 52, tile_w, 60, acc_total, '账号功能', _TEXT)
    _stat_tile(im, d, tx + 3 * (tile_w + 12), y + 52, tile_w, 60,
               f'{acc_ok}/{acc_total}' if acc_total else '0/0',
               '账号 成功/总数', _GREEN if acc_err == 0 else _GOLD)
    _kv_row(d, mx + 22, y + total_h - 30, (inner_w - 44) // 2 - 8,
            '上次查分', last_query, _TEXT_SOFT)
    _kv_row(d, mx + inner_w // 2 + 8, y + total_h - 30, (inner_w - 44) // 2 - 8,
            '上次分析', last_analysis, _TEXT_SOFT)
    y += total_h + 16

    # ---- 偏好 ----
    _card(im, (mx, y, mx + inner_w, y + pref_h), radius=18,
          fill=(255, 255, 255, 225), shadow=False)
    d.text((mx + 22, y + pref_h // 2),
           f'数据源 {src}', font=_font_bold(15), fill=_TEXT, anchor='lm')
    d.text((mx + inner_w // 2, y + pref_h // 2),
           f'B50 主题 {theme}', font=_font_bold(15), fill=_TEXT, anchor='mm')
    d.text((mx + inner_w - 22, y + pref_h // 2),
           f'数据存储 {"已开启" if storage else "未开启"}',
           font=_font_bold(15), fill=_GREEN if storage else _MUTED, anchor='rm')
    y += pref_h + 16

    # ---- 功能分布 / 发票 ----
    if distribution_h:
        _card(im, (mx, y, mx + inner_w, y + distribution_h), radius=18,
              fill=(255, 255, 255, 225))
        if operation_items:
            _section_title(d, mx + 22, y + 16, '账号功能分布', _ACCENT)
            _draw_operation_distribution(im, d, mx + 22, y, inner_w - 44, operation_items)
        if ticket_total:
            ticket_y = y + (292 if operation_items else 0)
            if operation_items:
                d.line((mx + 22, ticket_y, mx + inner_w - 22, ticket_y),
                       fill=(225, 230, 242, 255), width=2)
            success = int(ticket.get('success') or 0)
            error = int(ticket.get('error') or 0)
            d.text((mx + 22, ticket_y + 18), '发票结果',
                   font=_font_bold(16), fill=_TEXT)
            d.text((mx + inner_w - 22, ticket_y + 18),
                   f'成功 {success}  ·  失败 {error}',
                   font=_font_bold(14), fill=_TEXT_SOFT, anchor='rt')
            _bar(im, mx + 22, ticket_y + 48, inner_w - 44, 12,
                 success / ticket_total, _GREEN, bg=(244, 207, 214, 255), radius=6)
            d.text((mx + 22, ticket_y + 70),
                   f'成功率 {ticket.get("success_rate", 0)}%  ·  '
                   f'returnCode=0 {int(ticket.get("return_code_0") or 0)} 次  ·  '
                   f'未返回 {int(ticket.get("return_code_null") or 0)} 次',
                   font=_font_bold(13), fill=_TEXT_SOFT)
        y += distribution_h + 16

    # ---- 最近记录 ----
    if log_lines:
        card_h = 30 + log_lines * 26 + 16
        _card(im, (mx, y, mx + inner_w, y + card_h), radius=20,
              fill=(255, 255, 255, 225))
        _section_title(d, mx + 22, y + 14, '最近记录', _ACCENT)
        ly = y + 48
        for entry in recent_acc[:5]:
            ts = datetime.fromtimestamp(float(entry['created_at'])).strftime('%m-%d %H:%M')
            status = '成功' if entry.get('status') == 'success' else '失败'
            label = op_labels.get(str(entry.get('operation')),
                                  str(entry.get('operation')))
            col = _GREEN if entry.get('status') == 'success' else _RED
            d.text((mx + 22, ly), ts, font=_font_mono(13), fill=_MUTED)
            d.text((mx + 130, ly), label, font=_font_bold(14), fill=_TEXT)
            d.text((mx + 300, ly), status, font=_font_bold(14), fill=col)
            d.text((mx + inner_w - 22, ly), str(entry.get('ref_id', '')),
                   font=_font_mono(13), fill=_TEXT_SOFT, anchor='rt')
            ly += 26
        for entry in recent_break[:20]:
            ts = datetime.fromtimestamp(float(_g(entry, 'created_at') or 0)).strftime('%m-%d %H:%M')
            delta = int(_g(entry, 'delta') or 0)
            sign = '+' if delta >= 0 else ''
            reason = _g(entry, 'reason', '')
            if reason.startswith('free_window_exempt:'):
                base = reason.split(':', 1)[1]
                label = '免费窗口·' + reason_map.get(base, base)
            elif reason.startswith('freedom_exempt:'):
                base = reason.split(':', 1)[1]
                label = '免单·' + reason_map.get(base, base)
            elif reason.startswith('service:'):
                base = reason.split(':', 1)[1]
                label = service_labels.get(base, base)
            elif reason.startswith('once_reward:'):
                base = reason.split(':', 1)[1]
                label = '一次性奖励·' + once_reward_labels.get(base, base)
            else:
                label = reason_map.get(reason, reason)
            col = _GREEN if delta >= 0 else _RED
            d.text((mx + 22, ly), ts, font=_font_mono(13), fill=_MUTED)
            d.text((mx + 130, ly), label, font=_font_bold(14), fill=_TEXT)
            d.text((mx + inner_w - 22, ly), f'{sign}{delta} BREAK',
                   font=_font_mono(15), fill=col, anchor='rt')
            ly += 26
        y += card_h + 16

    _footer(im, width, canvas_h)
    return _finalize(im)


__all__ = ['render_awmc_profile']
