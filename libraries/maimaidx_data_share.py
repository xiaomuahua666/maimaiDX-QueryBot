"""玩家成绩脱敏共享偏好（默认开启，opt-out）。

成绩、Rating、推分趋势等可脱敏后用于同段统计（ARPI）、锐评样本与公开数据集。
用户发送「不同意共享我的数据」后，导出与聚合会排除其数据；个人功能不受影响。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Set

from loguru import logger as log

CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "data_share_config.json"

_lock = RLock()


class DataShareManager:
    """共享默认开启：仅记录主动 opt-out 的用户。"""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self.path = path
        self._ensure_config()

    def _ensure_config(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._save({"opted_out_users": [], "updated_at": time.time()})

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"opted_out_users": []}
            users = data.get("opted_out_users") or []
            if not isinstance(users, list):
                users = []
            data["opted_out_users"] = [str(u) for u in users]
            return data
        except Exception as e:
            log.error(f"[DataShare] 加载配置失败: {e}")
            return {"opted_out_users": []}

    def _save(self, config: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as e:
            log.error(f"[DataShare] 保存配置失败: {e}")

    def _opted_out_set(self) -> Set[str]:
        return set(self._load().get("opted_out_users") or [])

    def is_sharing_enabled(self, user_id: int | str) -> bool:
        """默认 True；仅 opt-out 用户为 False。"""
        return str(user_id) not in self._opted_out_set()

    def opt_out(self, user_id: int | str) -> bool:
        """拒绝共享。返回是否从「共享」变为「不共享」。"""
        uid = str(user_id)
        with _lock:
            config = self._load()
            opted = list(config.get("opted_out_users") or [])
            if uid in opted:
                return False
            opted.append(uid)
            config["opted_out_users"] = opted
            config["updated_at"] = time.time()
            self._save(config)
            self._invalidate_rank_course_stats()
            log.info(f"[DataShare] 用户 {uid} 已拒绝数据共享")
            return True

    def opt_in(self, user_id: int | str) -> bool:
        """重新同意共享。返回是否从「不共享」变为「共享」。"""
        uid = str(user_id)
        with _lock:
            config = self._load()
            opted = list(config.get("opted_out_users") or [])
            if uid not in opted:
                return False
            opted = [x for x in opted if x != uid]
            config["opted_out_users"] = opted
            config["updated_at"] = time.time()
            self._save(config)
            self._invalidate_rank_course_stats()
            log.info(f"[DataShare] 用户 {uid} 已重新同意数据共享")
            return True

    @staticmethod
    def _invalidate_rank_course_stats() -> None:
        try:
            from .maimaidx_rank_course import invalidate_course_stats

            invalidate_course_stats()
        except ImportError:
            pass

    def list_opted_out(self) -> List[str]:
        return sorted(self._opted_out_set())

    def status_text(self, user_id: int | str) -> str:
        if self.is_sharing_enabled(user_id):
            return (
                "当前数据共享：已开启（默认）\n"
                "成绩、Rating、推分趋势等将脱敏后用于同段统计、锐评优化与公开数据集。\n"
                "如需关闭，请发送「不同意共享我的数据」。"
            )
        return (
            "当前数据共享：已关闭\n"
            "你的成绩不会进入脱敏公开数据集与同段样本聚合。\n"
            "如需重新开启，请发送「同意共享我的数据」。"
        )


data_share = DataShareManager()
