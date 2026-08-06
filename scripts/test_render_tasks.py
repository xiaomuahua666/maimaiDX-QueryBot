#!/usr/bin/env python3
"""预渲染任务持久化与恢复状态最小回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nonebot_plugin_maimaidx.libraries import maimaidx_render_tasks as tasks


class RenderTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_file = tasks.TASK_FILE
        self._tmp = tempfile.TemporaryDirectory()
        tasks.TASK_FILE = Path(self._tmp.name) / "render_tasks.json"

    def tearDown(self) -> None:
        tasks.TASK_FILE = self._old_file
        self._tmp.cleanup()

    def test_progress_format_and_finish(self) -> None:
        task_id = tasks.start_task("chart", total=10, limit=10)
        tasks.update_task(
            task_id,
            processed=3,
            eta_seconds=65,
            message="正在渲染",
        )
        rendered = tasks.format_active_tasks()
        self.assertIn("谱面：进行中 3/10", rendered)
        self.assertIn("剩余 1分05秒", rendered)
        tasks.finish_task(task_id)
        self.assertEqual(tasks.active_tasks(), [])

    def test_interrupted_task_becomes_pending(self) -> None:
        tasks.start_task("audio", total=20, force=True)
        self.assertEqual(tasks.mark_interrupted_tasks_pending(), 1)
        pending = tasks.pending_tasks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "audio")
        self.assertIn("音频：等待恢复", tasks.format_active_tasks())


if __name__ == "__main__":
    unittest.main()
