# 谱面印象 API 客户端（对接 v.wmc.pub 公开 API）
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger as log

# v.wmc.pub 难度值：2=Basic, 3=Advanced, 4=Expert, 5=Master, 6=Re:Master
WMC_DIFF_NAMES = {2: "Basic", 3: "Advanced", 4: "Expert", 5: "Master", 6: "Re:Master"}


def make_chart_key(song_id: str, kind: str, diff: int) -> str:
    """构建 chartKey：{songId}:{kind}:{diff}，diff 取值 2-6。"""
    return f"{song_id}:{kind}:{diff}"


def build_preview_url(song_id: str, kind: str, diff: int) -> str:
    """生成谱面预览网页链接。"""
    return f"https://v.wmc.pub/?song={song_id}&kind={kind}&diff={diff}"


class WmcAPI:
    """v.wmc.pub 谱面印象 API 客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(url, headers=self._headers(), params=params)
                if r.status_code == 401:
                    log.warning("[wmc] API 认证失败，请检查 wmc_api_key 配置")
                    return None
                if r.status_code != 200:
                    log.warning(f"[wmc] API 非 200 path={path} status={r.status_code}")
                    return None
                return r.json()
        except Exception as e:
            log.warning(f"[wmc] 请求异常 path={path} err={type(e).__name__}: {e}")
            raise

    # ---------- 谱面列表 / 搜索 ----------

    async def search_charts(
        self,
        q: Optional[str] = None,
        kind: Optional[str] = None,
        difficulty: Optional[int] = None,
        page: int = 1,
        limit: int = 20,
    ) -> Optional[Dict[str, Any]]:
        """GET /charts — 搜索谱面列表。"""
        params: Dict[str, Any] = {"page": page, "limit": min(limit, 50)}
        if q:
            params["q"] = q
        if kind:
            params["kind"] = kind
        if difficulty:
            params["difficulty"] = difficulty
        return await self._get("/charts", params)

    # ---------- 单谱面详情 ----------

    async def get_chart(self, chart_key: str) -> Optional[Dict[str, Any]]:
        """GET /charts/:chartKey — 获取谱面详情。"""
        return await self._get(f"/charts/{chart_key}")

    # ---------- 评论 / 谱面印象 ----------

    async def get_comments(
        self,
        chart_key: str,
        page: int = 1,
        limit: int = 15,
    ) -> Optional[Dict[str, Any]]:
        """GET /charts/:chartKey/comments — 获取谱面评论列表。"""
        return await self._get(
            f"/charts/{chart_key}/comments",
            params={"page": page, "limit": limit},
        )

    # ---------- 评分统计 ----------

    async def get_ratings(self, chart_key: str) -> Optional[Dict[str, Any]]:
        """GET /charts/:chartKey/ratings — 获取评分统计。"""
        return await self._get(f"/charts/{chart_key}/ratings")

    # ---------- 成绩提交 ----------

    async def get_scores(
        self,
        chart_key: str,
        page: int = 1,
        limit: int = 15,
    ) -> Optional[Dict[str, Any]]:
        """GET /charts/:chartKey/scores — 获取成绩提交列表。"""
        return await self._get(
            f"/charts/{chart_key}/scores",
            params={"page": page, "limit": limit},
        )

    # ---------- 谱面标签（难度分析） ----------

    async def get_tags(
        self,
        chart_key: str,
        radar_threshold: int = 40,
        feature_threshold: float = 0.5,
    ) -> Optional[Dict[str, Any]]:
        """GET /charts/:chartKey/tags — 获取谱面标签摘要。"""
        return await self._get(
            f"/charts/{chart_key}/tags",
            params={"radar_threshold": radar_threshold, "feature_threshold": feature_threshold},
        )

    # ---------- 排行榜 ----------

    async def get_rankings(
        self,
        sort: str = "rating",
        limit: int = 10,
    ) -> Optional[Dict[str, Any]]:
        """GET /rankings — 获取排行榜。"""
        return await self._get("/rankings", params={"sort": sort, "limit": min(limit, 20)})

    # ---------- 全局统计 ----------

    async def get_stats(self) -> Optional[Dict[str, Any]]:
        """GET /stats — 获取全局统计。"""
        return await self._get("/stats")
