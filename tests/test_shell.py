"""
tests/test_shell.py - ShellTool の単体テスト
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.shell import run, ShellResult


class TestShellTool(unittest.TestCase):

    def test_simple_echo(self):
        """echo コマンドが成功し stdout に結果が入る。"""
        result = run("echo hello")
        self.assertTrue(result.success)
        self.assertEqual(result.stdout.strip(), "hello")
        self.assertEqual(result.returncode, 0)

    def test_failed_command(self):
        """存在しないファイルの cat は失敗する。"""
        result = run("cat /nonexistent_file_xyz")
        self.assertFalse(result.success)
        self.assertNotEqual(result.returncode, 0)

    def test_timeout(self):
        """タイムアウトが機能する。"""
        result = run("sleep 10", timeout=1)
        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)

    def test_multiline_output(self):
        """複数行の出力が正しく取得できる。"""
        result = run("printf 'line1\nline2\nline3'")
        self.assertTrue(result.success)
        lines = result.stdout.strip().split("\n")
        self.assertEqual(lines, ["line1", "line2", "line3"])

    def test_output_property_prefers_stdout(self):
        """output プロパティは stdout を優先する。"""
        result = run("echo out")
        self.assertEqual(result.output, "out")

    def test_stderr_captured(self):
        """stderr も取得できる。"""
        result = run("echo err >&2")
        self.assertIn("err", result.stderr)

    def test_pipe(self):
        """パイプが動作する。"""
        result = run("echo -e 'a\nb\nc' | wc -l")
        self.assertTrue(result.success)
        self.assertEqual(result.stdout.strip(), "3")

    def test_japanese_output(self):
        """日本語出力が文字化けしない。"""
        result = run("echo 'テスト'")
        self.assertTrue(result.success)
        self.assertIn("テスト", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
