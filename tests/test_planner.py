"""
tests/test_planner.py - TaskPlanner の単体テスト

Ollama への HTTP 呼び出しはモックで差し替え、
プラン生成・パース・バリデーションのみを検証する。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from planner import TaskPlanner, PlannerError


# ------------------------------------------------------------------ #
# モック OllamaClient
# ------------------------------------------------------------------ #

class MockOllamaClient:
    """generate() が常に固定のJSONを返すモック。"""

    def __init__(self, response: str):
        self._response = response
        self.called_with: list[str] = []

    def generate(self, prompt: str) -> str:
        self.called_with.append(prompt)
        return self._response


class ErrorOllamaClient:
    """generate() が常に例外を送出するモック。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def generate(self, prompt: str) -> str:
        raise self._exc


# ------------------------------------------------------------------ #
# テスト用JSONプラン
# ------------------------------------------------------------------ #

VALID_SHELL_PLAN = json.dumps({
    "task_summary": "ファイル一覧を確認する",
    "steps": [
        {
            "step_id": 1,
            "tool": "shell",
            "description": "Downloadsのファイル一覧を表示",
            "command": "ls -la ~/Downloads",
            "danger_level": 0,
            "on_error": "abort",
        }
    ],
})

VALID_FILESYSTEM_PLAN = json.dumps({
    "task_summary": "PDFをDocumentsに移動する",
    "steps": [
        {
            "step_id": 1,
            "tool": "shell",
            "description": "PDFを検索",
            "command": "find ~/Downloads -name '*.pdf'",
            "danger_level": 0,
            "on_error": "abort",
        },
        {
            "step_id": 2,
            "tool": "filesystem",
            "description": "PDFをDocumentsへ移動",
            "action": "move",
            "src": "~/Downloads/*.pdf",
            "dst": "~/Documents/",
            "danger_level": 1,
            "on_error": "abort",
        },
    ],
})


# ------------------------------------------------------------------ #
# テストクラス
# ------------------------------------------------------------------ #

class TestTaskPlannerParsing(unittest.TestCase):
    """正常系: パースと Plan オブジェクトの内容を検証。"""

    def test_shell_plan_parsed(self):
        planner = TaskPlanner(client=MockOllamaClient(VALID_SHELL_PLAN))
        plan = planner.plan("ファイル一覧を確認して")
        self.assertEqual(plan.task_summary, "ファイル一覧を確認する")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0]["tool"], "shell")
        self.assertEqual(plan.steps[0]["command"], "ls -la ~/Downloads")

    def test_filesystem_plan_parsed(self):
        planner = TaskPlanner(client=MockOllamaClient(VALID_FILESYSTEM_PLAN))
        plan = planner.plan("PDFをDocumentsに移動して")
        self.assertEqual(plan.task_summary, "PDFをDocumentsに移動する")
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[1]["tool"], "filesystem")
        self.assertEqual(plan.steps[1]["action"], "move")

    def test_raw_preserved(self):
        """raw フィールドに元のJSON文字列が保存される。"""
        planner = TaskPlanner(client=MockOllamaClient(VALID_SHELL_PLAN))
        plan = planner.plan("テスト")
        self.assertEqual(plan.raw, VALID_SHELL_PLAN)

    def test_prompt_contains_task(self):
        """plan() に渡したタスク文字列がプロンプトに含まれる。"""
        mock = MockOllamaClient(VALID_SHELL_PLAN)
        planner = TaskPlanner(client=mock)
        planner.plan("Downloadsを整理して")
        self.assertIn("Downloadsを整理して", mock.called_with[0])


class TestTaskPlannerValidation(unittest.TestCase):
    """バリデーション: 不正なJSONに対して PlannerError が送出される。"""

    def _planner(self, response: str) -> TaskPlanner:
        return TaskPlanner(client=MockOllamaClient(response))

    def test_invalid_json_raises(self):
        with self.assertRaises(PlannerError):
            self._planner("これはJSONじゃない").plan("テスト")

    def test_missing_task_summary_raises(self):
        bad = json.dumps({"steps": [
            {"step_id": 1, "tool": "shell", "description": "x",
             "command": "ls", "danger_level": 0, "on_error": "abort"}
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_missing_steps_raises(self):
        bad = json.dumps({"task_summary": "テスト"})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_empty_steps_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": []})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_shell_missing_command_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"step_id": 1, "tool": "shell", "description": "x",
             "danger_level": 0, "on_error": "abort"}
            # command なし
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_filesystem_missing_action_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"step_id": 1, "tool": "filesystem", "description": "x",
             "src": "~/file.txt", "danger_level": 1, "on_error": "abort"}
            # action なし
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_filesystem_move_missing_dst_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"step_id": 1, "tool": "filesystem", "description": "x",
             "action": "move", "src": "~/file.txt",
             "danger_level": 1, "on_error": "abort"}
            # dst なし
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_invalid_danger_level_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"step_id": 1, "tool": "shell", "description": "x",
             "command": "ls", "danger_level": 99, "on_error": "abort"}
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_invalid_tool_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"step_id": 1, "tool": "browser", "description": "x",
             "danger_level": 0, "on_error": "abort"}
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")


class TestTaskPlannerConnectionError(unittest.TestCase):
    """Ollama 接続失敗時に PlannerError が送出される。"""

    def test_url_error_raises_planner_error(self):
        import urllib.error
        client = ErrorOllamaClient(
            urllib.error.URLError("Connection refused")
        )
        planner = TaskPlanner(client=client)
        with self.assertRaises(PlannerError) as ctx:
            planner.plan("テスト")
        self.assertIn("接続できませんでした", str(ctx.exception))

    def test_generic_error_raises_planner_error(self):
        client = ErrorOllamaClient(RuntimeError("unexpected"))
        planner = TaskPlanner(client=client)
        with self.assertRaises(PlannerError):
            planner.plan("テスト")


if __name__ == "__main__":
    unittest.main(verbosity=2)
