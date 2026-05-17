"""
tests/test_logger.py - AuditLogger の単体テスト
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from logger import AuditLogger


class TestAuditLogger(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.logger = AuditLogger(log_dir=self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _read_entries(self) -> list[dict]:
        log_path = self.logger.get_log_path()
        entries = []
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def test_log_file_created(self):
        self.logger.append_step(
            task_summary="テスト", step_id=1, tool="shell",
            danger_level=0, status="success", command="ls", output="file.txt",
        )
        self.assertTrue(self.logger.get_log_path().exists())

    def test_shell_step_fields(self):
        self.logger.append_step(
            task_summary="PDFを移動", step_id=1, tool="shell",
            danger_level=0, status="success",
            command="ls -la ~/Downloads", output="file1.pdf\nfile2.pdf",
        )
        entries = self._read_entries()
        e = entries[0]
        self.assertEqual(e["task_summary"], "PDFを移動")
        self.assertEqual(e["tool"], "shell")
        self.assertIn("timestamp", e)
        self.assertNotIn("user_confirmed", e)

    def test_multiple_steps(self):
        for i in range(1, 4):
            self.logger.append_step(
                task_summary="複数ステップ", step_id=i, tool="shell",
                danger_level=0, status="success", command=f"echo step{i}",
            )
        entries = self._read_entries()
        self.assertEqual([e["step_id"] for e in entries], [1, 2, 3])

    def test_task_start_end_events(self):
        self.logger.append_task_start("テストタスク")
        self.logger.append_step(
            task_summary="テストタスク", step_id=1, tool="shell",
            danger_level=0, status="success",
        )
        self.logger.append_task_end("テストタスク", "completed")
        entries = self._read_entries()
        self.assertEqual(entries[0]["event"], "task_start")
        self.assertEqual(entries[-1]["event"], "task_end")

    def test_none_fields_excluded(self):
        self.logger.append_step(
            task_summary="最小", step_id=1, tool="shell",
            danger_level=0, status="success",
        )
        e = self._read_entries()[0]
        for field in ("action", "src", "dst", "command", "output", "error", "user_confirmed"):
            self.assertNotIn(field, e)

    def test_japanese_characters(self):
        self.logger.append_step(
            task_summary="ダウンロードフォルダを整理する", step_id=1,
            tool="filesystem", danger_level=1, status="success",
            action="move", src="~/ダウンロード/*.pdf", dst="~/ドキュメント/",
            user_confirmed=True,
        )
        entries = self._read_entries()
        self.assertEqual(entries[0]["task_summary"], "ダウンロードフォルダを整理する")

    def test_jsonl_format(self):
        for i in range(1, 4):
            self.logger.append_step(
                task_summary="JSONL確認", step_id=i, tool="shell",
                danger_level=0, status="success",
            )
        log_path = self.logger.get_log_path()
        with open(log_path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
