from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StyleSpec:
    raw: str = ""
    direction: str = ""
    tone: str = "自然、熟悉舞萌的朋友口吻"
    sharpness: int = 3
    warmth: int = 3
    humor: int = 2
    address: str = ""
    suffix: str = ""
    focus: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    label: str
    value: str
    source: str
    confidence: str = "high"


@dataclass(frozen=True)
class Candidate:
    song_id: str
    title: str
    level: str
    ds: float
    achievement: float
    estimated_gain: int
    target: str
    reason: str
    cover_path: str = ""
    artist: str = ""
    genre: str = ""
    level_index: int = 0
    chart_type: str = "SD"
    pool: str = "old"
    target_achievement: float = 100.0
    current_ra: int = 0
    target_ra: int = 0
    priority_score: float = 0.0
    route_step: int = 0
    cumulative_gain: int = 0
    risk: str = "稳妥"


@dataclass
class EvidencePack:
    nickname: str
    rating: int
    b35: list[dict[str, Any]] = field(default_factory=list)
    b15: list[dict[str, Any]] = field(default_factory=list)
    all_charts: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    peer: dict[str, Any] = field(default_factory=dict)
    ds_bands: list[dict[str, Any]] = field(default_factory=list)
    difficulty_bands: list[dict[str, Any]] = field(default_factory=list)
    genre_profiles: list[dict[str, Any]] = field(default_factory=list)
    song_groups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    trend: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoastReport:
    headline: str
    summary: str
    analysis: str
    strengths: list[str]
    weaknesses: list[str]
    peer_takeaways: list[str]
    actions: list[str]
    recommendations: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    style: StyleSpec
