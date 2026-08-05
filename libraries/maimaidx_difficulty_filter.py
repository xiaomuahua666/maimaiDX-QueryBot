"""
难度筛选器：统一封装 maimai DX 难度/定数筛选逻辑。

支持：
- 难度等级筛选（绿/黄/红/紫/白，或 Basic/Advanced/Expert/Master/Re:MASTER）
- 定数筛选（如 13, 13+, 14.5）
- 定数范围筛选（如 13-14）
- 组合条件（难度等级 + 定数）

用法：
    filter = DifficultyFilter.parse("紫13+")
    if filter.matches(record):
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple


log = logging.getLogger("nonebot_plugin_maimaidx.difficulty_filter")


class _RecordLike(Protocol):
    level_index: int
    ds: float


# 难度等级映射：多种别名 -> level_index (0~4)
_DIFFICULTY_ALIASES: Dict[str, int] = {
    # 中文颜色
    "绿": 0, "綠": 0,
    "黄": 1, "黃": 1,
    "红": 2, "紅": 2,
    "紫": 3,
    "白": 4,
    # 英文全称（大小写不敏感）
    "basic": 0, "advanced": 1, "expert": 2, "master": 3, "remaster": 4,
    "re:master": 4, "re_master": 4,
    # 英文首字母
    "b": 0, "a": 1, "e": 2, "m": 3, "r": 4,
    # 数字索引（仅允许完整匹配，避免与定数如 14/13+ 冲突；见 parse）
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
}

# 难度名称（用于展示）
_DIFFICULTY_NAMES: List[str] = ["Basic", "Advanced", "Expert", "Master", "Re:MASTER"]
_DIFFICULTY_COLORS: List[str] = ["绿", "黄", "红", "紫", "白"]
_STAR_ALIASES: Dict[str, int] = {
    "一星": 1, "一颗星": 1, "1星": 1,
    "二星": 2, "二颗星": 2, "2星": 2,
    "三星": 3, "三颗星": 3, "3星": 3,
    "四星": 4, "四颗星": 4, "4星": 4,
    "五星": 5, "五颗星": 5, "5星": 5,
}


def _normalize_difficulty_input(text: str) -> str:
    """统一难度输入：去空格、转小写、统一 remaster 变体。"""
    text = text.strip().lower()
    text = text.replace("re：master", "re:master")
    text = text.replace("re_master", "re:master")
    return text


@dataclass(frozen=True)
class DifficultyFilter:
    """
    不可变的难度筛选条件。

    字段：
        level_index:   难度等级 0~4（None 表示不限）
        ds_exact:      精确定数（None 表示不限）
        ds_min:        定数下限（None 表示不限）
        ds_max:        定数上限（None 表示不限）
        tolerance:     定数匹配容差（默认 0.05，用于精确匹配）

    定数解析规则（maimai DX 约定）：
        - "14"   -> 等级 14 的谱面（定数范围 [14.0, 14.6]）
        - "14+"  -> 等级 14+ 的谱面（定数范围 [14.7, 14.9]）
        - "14.0" -> 精确匹配定数 14.0
        - "14.5" -> 精确匹配定数 14.5
        - "13-14"-> 定数范围 [13.0, 14.0]
    """
    level_index: Optional[int] = None
    star_count: Optional[int] = None
    ds_exact: Optional[float] = None
    ds_min: Optional[float] = None
    ds_max: Optional[float] = None
    tolerance: float = 0.05

    # 缓存解析后的展示名称，避免重复计算
    _display_name: Optional[str] = field(default=None, repr=False, compare=False)

    # maimai DX 等级与定数范围映射（非 + 等级上限为 .6，+ 等级下限为 .7）
    _DS_PLUS_THRESHOLD: float = 0.7  # + 等级的定数下限

    @classmethod
    def parse(cls, text: str, *, tolerance: float = 0.05) -> DifficultyFilter:
        """
        从字符串解析筛选条件。

        支持的格式：
            - "紫" / "master" / "3"          -> 仅难度等级
            - "13" / "13+" / "14.5"         -> 定数筛选（见上方规则）
            - "13-14" / "12.5-13.5"         -> 定数范围
            - "紫13" / "master 14+"          -> 难度等级 + 定数
            - "紫13-14"                      -> 难度等级 + 定数范围

        示例：
            >>> DifficultyFilter.parse("紫")
            DifficultyFilter(level_index=3)
            >>> DifficultyFilter.parse("14")      # 等级14
            DifficultyFilter(ds_min=14.0, ds_max=14.6)
            >>> DifficultyFilter.parse("14+")     # 等级14+
            DifficultyFilter(ds_min=14.7, ds_max=14.9)
            >>> DifficultyFilter.parse("14.0")    # 精确定数14.0
            DifficultyFilter(ds_exact=14.0)
            >>> DifficultyFilter.parse("紫14+")
            DifficultyFilter(level_index=3, ds_min=14.7, ds_max=14.9)
        """
        text = text.strip()
        if not text:
            raise ValueError("难度筛选条件不能为空")

        normalized = _normalize_difficulty_input(text)

        level_index: Optional[int] = None
        star_count: Optional[int] = None
        remaining = normalized

        # 星数是 DX 分数星级，与谱面难度/等级完全独立。
        for alias, count in sorted(_STAR_ALIASES.items(), key=lambda x: -len(x[0])):
            if normalized.startswith(alias):
                star_count = count
                remaining = normalized[len(alias):].strip()
                break

        # 先尝试提取难度前缀（最长匹配优先）
        # 注意：数字索引 0~4 只能完整匹配，不能做前缀匹配，否则 "14" 会被误认为难度=Advanced(1)
        prefix_aliases = {k: v for k, v in _DIFFICULTY_ALIASES.items() if not k.isdigit()}
        sorted_aliases = sorted(prefix_aliases.items(), key=lambda x: -len(x[0]))
        if star_count is None:
            for alias, idx in sorted_aliases:
                if normalized.startswith(alias):
                    level_index = idx
                    remaining = normalized[len(alias):].strip()
                    break

        # 如果没有匹配到难度前缀，且整个字符串是难度别名
        if star_count is None and level_index is None and normalized in _DIFFICULTY_ALIASES:
            level_index = _DIFFICULTY_ALIASES[normalized]
            remaining = ""

        # 解析剩余部分的定数信息
        ds_exact, ds_min, ds_max, parsed_ds = cls._parse_ds_part(remaining)

        # 关键：避免无法解析的输入静默变成「全部」导致误筛选
        if level_index is None and star_count is None and not parsed_ds:
            raise ValueError(f"无法解析难度/定数：{text}")
        if remaining and not parsed_ds:
            raise ValueError(f"无法解析定数部分：{remaining}")

        # 构建展示名称
        display_parts: List[str] = []
        if level_index is not None:
            display_parts.append(f"{_DIFFICULTY_COLORS[level_index]}({_DIFFICULTY_NAMES[level_index]})")
        if star_count is not None:
            display_parts.append(f"DX星数={star_count}")
        if ds_exact is not None:
            display_parts.append(f"定数={ds_exact}")
        elif ds_min is not None or ds_max is not None:
            range_str = f"{ds_min or ''}-{ds_max or ''}".strip("-")
            display_parts.append(f"定数[{range_str}]")

        display_name = "".join(display_parts) if display_parts else "全部"

        result = cls(
            level_index=level_index,
            star_count=star_count,
            ds_exact=ds_exact,
            ds_min=ds_min,
            ds_max=ds_max,
            tolerance=tolerance,
            _display_name=display_name,
        )
        return result

    @staticmethod
    def _parse_ds_part(text: str) -> Tuple[Optional[float], Optional[float], Optional[float], bool]:
        """
        解析定数字符串部分。

        规则：
            - "14"    -> 等级 14（ds_min=14.0, ds_max=14.6）
            - "14+"   -> 等级 14+（ds_min=14.7, ds_max=14.9）
            - "14.0"  -> 精确定数 14.0（ds_exact=14.0）
            - "14.5"  -> 精确定数 14.5（ds_exact=14.5）
            - "13-14" -> 定数范围 [13.0, 14.0]

        返回: (ds_exact, ds_min, ds_max, parsed)
        """
        text = text.strip()
        if not text:
            return None, None, None, False

        # 范围格式: "13-14", "12.5-13.5"
        range_match = re.match(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$", text)
        if range_match:
            ds_min = float(range_match.group(1))
            ds_max = float(range_match.group(2))
            return None, ds_min, ds_max, True

        # 单一定数/等级: "13", "13+", "14.5"
        single_match = re.match(r"^(\d+(?:\.\d+)?)(\+?)$", text)
        if single_match:
            base = float(single_match.group(1))
            has_plus = single_match.group(2) == "+"

            # 关键区分：带小数点的是精确定数，不带小数点的是等级
            is_exact = "." in text.replace("+", "")
            if is_exact:
                # 精确定数：如 14.0, 14.5
                return base, None, None, True
            else:
                # 等级：如 14, 14+
                if has_plus:
                    # 14+ -> [14.7, 14.9]
                    return None, base + 0.7, base + 0.9, True
                else:
                    # 14 -> [14.0, 14.6]
                    return None, base, base + 0.6, True

        return None, None, None, False

    def matches(self, record: _RecordLike) -> bool:
        """判断单条成绩是否匹配筛选条件。"""
        # 检查难度等级
        if self.level_index is not None and record.level_index != self.level_index:
            return False

        # 检查精确定数（容差范围内）
        if self.ds_exact is not None:
            record_ds = float(record.ds or 0)
            if abs(record_ds - self.ds_exact) > (self.tolerance + 1e-9):
                return False

        # 检查定数范围
        if self.ds_min is not None or self.ds_max is not None:
            record_ds = float(record.ds or 0)
            if self.ds_min is not None and record_ds < self.ds_min:
                return False
            if self.ds_max is not None and record_ds > self.ds_max:
                return False

        return True

    def filter_records(self, records: List[_RecordLike]) -> List[_RecordLike]:
        """筛选成绩列表，返回匹配的记录。"""
        return [r for r in records if self.matches(r)]

    @property
    def display_name(self) -> str:
        """获取人类可读的筛选条件描述。"""
        return self._display_name or "全部"

    def __str__(self) -> str:
        return self.display_name


# 预定义常用筛选器，便于快速使用
class DifficultyPresets:
    """常用难度筛选预设。"""

    @staticmethod
    def green() -> DifficultyFilter:
        return DifficultyFilter(level_index=0)

    @staticmethod
    def yellow() -> DifficultyFilter:
        return DifficultyFilter(level_index=1)

    @staticmethod
    def red() -> DifficultyFilter:
        return DifficultyFilter(level_index=2)

    @staticmethod
    def purple() -> DifficultyFilter:
        return DifficultyFilter(level_index=3)

    @staticmethod
    def white() -> DifficultyFilter:
        return DifficultyFilter(level_index=4)

    @staticmethod
    def by_ds_exact(ds: float, tolerance: float = 0.5) -> DifficultyFilter:
        return DifficultyFilter(ds_exact=ds, tolerance=tolerance)

    @staticmethod
    def by_ds_range(ds_min: Optional[float] = None, ds_max: Optional[float] = None) -> DifficultyFilter:
        return DifficultyFilter(ds_min=ds_min, ds_max=ds_max)
