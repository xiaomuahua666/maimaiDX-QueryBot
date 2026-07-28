import re
from pathlib import Path

p = Path("libraries/maimaidx_rank_course.py")
content = p.read_text("utf-8")

# Let's fix the caller of _draw_sample_distribution first
content = content.replace(
"""    _draw_sample_distribution(
        draw, track, (240, y + 218, 1032, y + 230), color
    )""",
"""    _draw_sample_distribution(
        draw, track, (240, y + 218, 1032, y + 230), color, theme
    )"""
)
# Note: I already changed the signature of _draw_sample_distribution, but wait, the caller in draw_rank_course wasn't fully updated! 
# Let me just use regex to fix it safely.
