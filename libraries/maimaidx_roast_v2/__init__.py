"""Independent B50 roast engine.

The V2 boundary deliberately keeps platform/data adapters small and owns the
analysis, style, validation, rendering and footer experience in this package.
"""

from .analysis import build_evidence_pack, build_report_fallback
from .model import generate_report
from .policy import normalize_style, scan_text
from .render import render_report
from .snapshot import fetch_snapshot

__all__ = [
    "build_evidence_pack",
    "build_report_fallback",
    "fetch_snapshot",
    "generate_report",
    "normalize_style",
    "render_report",
    "scan_text",
]
