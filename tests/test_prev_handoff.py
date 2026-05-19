"""
tests/test_prev_handoff.py - $prev / capture_output ステップ間データ引き渡しのテスト

Section 16 (B-1) の実装を検証する:
  - capture_output=true のシェルステップが出力を保持する
  - src="$prev" のfilesystemステップが前ステップの出力を受け取る
  - capture_output がないステップは prev_output をリセットする
  - prev_output が None のときに $prev を使うとエラーになる
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from planner import Plan


# ------------------------------------------------------------------ #
# ヘルパー: テスト用の Plan を組み立てる
# ------------------------------------------------------------------ #

def make_plan(steps: list[dict]) -> Plan:
    return Plan(
        task_summary="テスト用タスク",
        steps=steps,
        raw="{}",
    )


# ------------------------------------------------------------------ #
# execute_step のモック設定
# ------------------------------------------------------------------ #

def _mock_execute_step(step: dict, timeout: int = 30):
    """
    テスト用 execute_step モック。
    step に "_mock_output" キーがあればそれを返す。
    デフォルトは (True, "", None)。
    """
    output = step.get("_mock_output", "")
    return (True, output, None)


def _mock_execute_step_fail(step: dict, timeout: int = 30):
    return (False, "", "コマンド失敗")


# ------------------------------------------------------------------ #
# テスト本体
# ------------------------------------------------------------------ #

class TestPrevHandoff(unittest.TestCase):
    """$prev / capture_output の executor ループ動作テスト。"""

    def _run_executor_loop(
        self,
        steps: list[dict],
        mock_execute: callable = _mock_execute_step,
    ) -> int:
        """
        run() 全体ではなく executor ループ部分だけを実行するヘルパー。
        AuditLogger・SafetyChecker・TaskPlanner・HITL 確認をすべてモックし、
        実際のファイルシステムに触れない状態でループを走らせる。
        """
        plan = make_plan(steps)

        # Safety Check が各ステップに _blocked=False を付けることを模倣
        for step in steps:
            step.setdefault("danger_level", 0)
            step["_blocked"] = False
            step["_safe_reason"] = ""

        mock_logger = MagicMock()
        mock_logger.get_log_path.return_value = "/tmp/test.log"

        with (
            patch("main.execute_step", side_effect=mock_execute),
            patch("main.AuditLogger", return_value=mock_logger),
            patch("main.SafetyChecker") as mock_checker_cls,
            patch("main.TaskPlanner") as mock_planner_cls,
            patch("main.ask_confirmation", return_value=True),
        ):
            mock_checker = MagicMock()
            mock_checker.timeout = 30
            # Safety Check: 各ステップの check() は元の danger_level を返すだけ
            def fake_check(step):
                r = MagicMock()
                r.danger_level = step.get("danger_level", 0)
                r.blocked = False
                r.reason = ""
                return r
            mock_checker.check.side_effect = fake_check
            mock_checker_cls.return_value = mock_checker

            mock_planner = MagicMock()
            mock_planner.plan.return_value = plan
            mock_planner_cls.return_value = mock_planner

            return main.run("テスト用タスク", dry_run=False, yes=True)

    # -------------------------------------------------------------- #

    def test_capture_output_passes_to_next_step(self):
        """
        capture_output=true のステップの出力が、
        次の src="$prev" ステップに正しく渡されること。
        """
        steps = [
            {
                "step_id": 1,
                "tool": "shell",
                "command": "find ~/Downloads -name '*.pdf' -mtime +30",
                "description": "古いPDFを検索",
                "capture_output": True,
                "_mock_output": "/home/user/Downloads/old.pdf\n/home/user/Downloads/old2.pdf",
                "on_error": "abort",
            },
            {
                "step_id": 2,
                "tool": "filesystem",
                "action": "move",
                "src": "$prev",
                "dst": "~/Documents/",
                "description": "古いPDFを移動",
                "on_error": "abort",
            },
        ]

        with patch("main.execute_step", side_effect=_mock_execute_step) as mock_exec:
            result = self._run_executor_loop(steps, mock_execute=_mock_execute_step)

        self.assertEqual(result, 0)

    def test_src_replaced_with_stripped_prev_output(self):
        """
        $prev が前ステップの出力（strip済み）で置換されること。
        """
        captured_output = "/home/user/Downloads/old.pdf\n  "  # 末尾に空白
        step2 = {
            "step_id": 2,
            "tool": "filesystem",
            "action": "move",
            "src": "$prev",
            "dst": "~/Documents/",
            "description": "移動",
            "on_error": "abort",
        }
        steps = [
            {
                "step_id": 1,
                "tool": "shell",
                "command": "find ...",
                "description": "検索",
                "capture_output": True,
                "_mock_output": captured_output,
                "on_error": "abort",
            },
            step2,
        ]

        executed_step2_src = []

        def capturing_execute(step, timeout=30):
            if step.get("step_id") == 2:
                executed_step2_src.append(step.get("src"))
            return _mock_execute_step(step, timeout)

        self._run_executor_loop(steps, mock_execute=capturing_execute)

        # strip() されていること
        self.assertEqual(len(executed_step2_src), 1)
        self.assertEqual(executed_step2_src[0], captured_output.strip())

    def test_prev_output_resets_when_no_capture_flag(self):
        """
        capture_output のないステップの後では prev_output がリセットされ、
        その後の $prev は None になりエラーになること。
        """
        steps = [
            {
                "step_id": 1,
                "tool": "shell",
                "command": "echo hello",
                "description": "出力あり（capture_outputなし）",
                # capture_output を意図的に省略
                "_mock_output": "hello",
                "on_error": "abort",
            },
            {
                "step_id": 2,
                "tool": "filesystem",
                "action": "move",
                "src": "$prev",  # prev_output は None のはず
                "dst": "~/Documents/",
                "description": "移動（失敗するはず）",
                "on_error": "abort",
            },
        ]

        result = self._run_executor_loop(steps)
        # prev_output が None なので return 1 になること
        self.assertEqual(result, 1)

    def test_prev_none_when_no_prior_capture(self):
        """
        最初のステップから src="$prev" を使うとエラーになること。
        """
        steps = [
            {
                "step_id": 1,
                "tool": "filesystem",
                "action": "move",
                "src": "$prev",
                "dst": "~/Documents/",
                "description": "先頭から $prev を使う（失敗するはず）",
                "on_error": "abort",
            },
        ]

        result = self._run_executor_loop(steps)
        self.assertEqual(result, 1)

    def test_no_prev_no_effect(self):
        """
        $prev を使わない通常の複数ステップは正常に完了すること。
        """
        steps = [
            {
                "step_id": 1,
                "tool": "shell",
                "command": "echo hello",
                "description": "echo",
                "_mock_output": "hello",
                "on_error": "abort",
            },
            {
                "step_id": 2,
                "tool": "shell",
                "command": "echo world",
                "description": "echo2",
                "_mock_output": "world",
                "on_error": "abort",
            },
        ]

        result = self._run_executor_loop(steps)
        self.assertEqual(result, 0)

    def test_capture_output_does_not_propagate_past_non_capture_step(self):
        """
        3ステップ構成:
          Step1: capture_output=True → 出力あり
          Step2: capture_output なし → prev_output リセット
          Step3: src="$prev" → エラーになること
        """
        steps = [
            {
                "step_id": 1,
                "tool": "shell",
                "command": "find ...",
                "description": "検索",
                "capture_output": True,
                "_mock_output": "/home/user/a.pdf",
                "on_error": "abort",
            },
            {
                "step_id": 2,
                "tool": "shell",
                "command": "echo done",
                "description": "中間ステップ（captureなし）",
                "_mock_output": "done",
                "on_error": "abort",
            },
            {
                "step_id": 3,
                "tool": "filesystem",
                "action": "move",
                "src": "$prev",
                "dst": "~/Documents/",
                "description": "移動（失敗するはず）",
                "on_error": "abort",
            },
        ]

        result = self._run_executor_loop(steps)
        self.assertEqual(result, 1)


# ------------------------------------------------------------------ #
# SYSTEM_PROMPT の $prev 記述テスト
# ------------------------------------------------------------------ #

class TestSystemPromptPrevDocs(unittest.TestCase):
    """SYSTEM_PROMPT に $prev / capture_output の説明が含まれること。"""

    def test_system_prompt_contains_capture_output(self):
        from planner import SYSTEM_PROMPT
        self.assertIn("capture_output", SYSTEM_PROMPT)

    def test_system_prompt_contains_prev(self):
        from planner import SYSTEM_PROMPT
        self.assertIn("$prev", SYSTEM_PROMPT)

    def test_system_prompt_contains_mtime_example(self):
        """条件付き移動の例（find -mtime）が含まれること。"""
        from planner import SYSTEM_PROMPT
        self.assertIn("mtime", SYSTEM_PROMPT)

    def test_system_prompt_prev_usage_note(self):
        """$prev は src にのみ使う旨の注意が含まれること。"""
        from planner import SYSTEM_PROMPT
        self.assertIn("src", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
