"""
tests/test_e2e.py - main.run() の統合テスト（end-to-end）

main.py を通した全体フローを dry-run ベースで検証する。
Ollama / 実ファイル操作は不要。

テスト対象フロー（設計書 Section 4）:
    Planner → Safety → 表示 → HITL → Executor → Logger

モック戦略:
    - TaskPlanner.plan()  : Ollama不要。テスト用プランを直接注入。
    - main.execute_step() : 実行不要。(True, "", None) を返すスタブ。
    - input()             : HITL確認をコントロール。
    - AuditLogger         : ログファイルへの書き込みを抑制。

テストケース一覧:
    E2E-01  dry-run: level0 のみ → 表示のみ、exitcode=0
    E2E-02  dry-run: level1 を含む → 表示のみ、exitcode=0
    E2E-03  dry-run: blocked ステップを含む → 即中断、exitcode=1
    E2E-04  実行: level0 のみ → 確認なし自動実行、exitcode=0
    E2E-05  実行: level1 + --yes フラグ → 確認スキップ、exitcode=0
    E2E-06  実行: level1 + --yes なし + ユーザーが y → 確認 → 実行、exitcode=0
    E2E-07  実行: level1 + --yes なし + ユーザーが n → キャンセル、exitcode=0
    E2E-08  実行: level2 + --yes フラグ → 確認必須（スキップ不可）→ y → exitcode=0
    E2E-09  実行: level2 + --yes フラグ + ユーザーが n → キャンセル、exitcode=0
    E2E-10  実行: ステップ失敗 → abort、exitcode=1
    E2E-11  実行: PlannerError → exitcode=1
    E2E-12  実行: $prev 引き渡し（単一ファイル）→ exitcode=0
    E2E-13  実行: $prev 引き渡し（複数ファイル）→ 1件ずつ実行、exitcode=0
    E2E-14  実行: $prev が None のときに $prev を使う → exitcode=1
    E2E-15  実行: $prev の出力が空 → スキップして正常終了、exitcode=0
    E2E-16  dry-run: blocked が複数あっても先頭だけで中断、exitcode=1
"""

from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from planner import Plan, PlannerError


# ------------------------------------------------------------------ #
# ヘルパー
# ------------------------------------------------------------------ #

def make_plan(steps: list[dict], task_summary: str = "テストタスク") -> Plan:
    """テスト用 Plan を組み立てる。"""
    return Plan(task_summary=task_summary, steps=steps, raw="{}")


def make_step(
    step_id: int,
    tool: str = "shell",
    danger_level: int = 0,
    command: str = "echo hello",
    capture_output: bool = False,
    src: str | None = None,
    dst: str | None = None,
    action: str | None = None,
    mock_output: str = "",
) -> dict:
    """テスト用ステップ辞書を組み立てる。"""
    step: dict = {
        "step_id": step_id,
        "tool": tool,
        "description": f"ステップ{step_id}の説明",
        "danger_level": danger_level,
        "on_error": "abort",
    }
    if tool == "shell":
        step["command"] = command
        if capture_output:
            step["capture_output"] = True
    if tool == "filesystem":
        step["action"] = action or "move"
        if src is not None:
            step["src"] = src
        if dst is not None:
            step["dst"] = dst
    step["_mock_output"] = mock_output
    return step


def mock_execute_step(step: dict, timeout: int = 30) -> tuple[bool, str, str | None]:
    """常に成功を返すスタブ。step["_mock_output"] を output として返す。"""
    return (True, step.get("_mock_output", ""), None)


def mock_execute_step_fail(step: dict, timeout: int = 30) -> tuple[bool, str, str | None]:
    """常に失敗を返すスタブ。"""
    return (False, "", "スタブ: コマンド失敗")


# ------------------------------------------------------------------ #
# Safety Checker をパスするためのデフォルト mock
# ------------------------------------------------------------------ #

def _default_safety_result(danger_level: int = 0, blocked: bool = False) -> MagicMock:
    r = MagicMock()
    r.danger_level = danger_level
    r.blocked = blocked
    r.reason = "ブロック理由" if blocked else ""
    return r


def _make_safety_mock(steps: list[dict]) -> MagicMock:
    """
    SafetyChecker のモック。
    各ステップの danger_level / blocked を step 辞書から読んで返す。
    ステップに "_blocked" キーがあれば blocked=True として扱う。
    """
    checker = MagicMock()
    checker.timeout = 30

    def check_side_effect(step: dict):
        r = MagicMock()
        r.danger_level = step.get("danger_level", 0)
        r.blocked      = step.get("_blocked", False)
        r.reason       = "ブロック理由" if r.blocked else ""
        return r

    checker.check.side_effect = check_side_effect
    return checker


# ------------------------------------------------------------------ #
# AuditLogger モック
# ------------------------------------------------------------------ #

def _make_logger_mock() -> MagicMock:
    logger = MagicMock()
    logger.get_log_path.return_value = "/tmp/test.jsonl"
    return logger


# ------------------------------------------------------------------ #
# テスト本体
# ------------------------------------------------------------------ #

class TestE2EDryRun(unittest.TestCase):
    """dry-run モードの統合テスト（Executor は呼ばれない）。"""

    def _run_with_plan(self, steps: list[dict], dry_run: bool = True, yes: bool = False):
        """
        run() をモック環境で実行し、exitcode を返す。
        stdout は抑制しない（テスト中に見えると便利なため）。
        """
        plan = make_plan(steps)
        logger_mock = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", side_effect=mock_execute_step):

            MockPlanner.return_value.plan.return_value = plan
            result = main.run("テストタスク", dry_run=dry_run, yes=yes)

        return result

    # E2E-01
    def test_dry_run_level0_only(self):
        """dry-run: level0 ステップのみ → exitcode=0、execute_step 未呼び出し。"""
        steps = [make_step(1, danger_level=0)]
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=True)

        self.assertEqual(code, 0)
        exec_mock.assert_not_called()

    # E2E-02
    def test_dry_run_with_level1(self):
        """dry-run: level1 ステップを含む → exitcode=0、execute_step 未呼び出し。"""
        steps = [
            make_step(1, danger_level=0),
            make_step(2, danger_level=1),
        ]
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=True)

        self.assertEqual(code, 0)
        exec_mock.assert_not_called()

    # E2E-03
    def test_dry_run_blocked_step_aborts(self):
        """dry-run: blocked ステップがあれば dry-run に到達する前に中断 → exitcode=1。"""
        steps = [
            make_step(1, danger_level=3),
        ]
        steps[0]["_blocked"] = True
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", side_effect=mock_execute_step), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=True)

        self.assertEqual(code, 1)

    # E2E-16
    def test_dry_run_multiple_blocked_stops_at_first(self):
        """blocked が複数あっても先頭のブロックで中断し exitcode=1。"""
        steps = [
            {**make_step(1, danger_level=3), "_blocked": True},
            {**make_step(2, danger_level=3), "_blocked": True},
        ]
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", side_effect=mock_execute_step), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=True)

        self.assertEqual(code, 1)
        # logger にはブロックされたステップが1件だけ記録される
        logger_mock.append_step.assert_called_once()


class TestE2EExecution(unittest.TestCase):
    """実行モードの統合テスト。execute_step はスタブ。"""

    def _run(
        self,
        steps: list[dict],
        yes: bool = False,
        user_input: str = "n",
        exec_side_effect=None,
    ) -> int:
        plan         = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=exec_side_effect or mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("builtins.input", return_value=user_input), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=yes)

        return code

    # E2E-04
    def test_level0_auto_execute_no_confirmation(self):
        """level0 のみ → input() 未呼び出し・自動実行 → exitcode=0。"""
        steps = [make_step(1, danger_level=0)]
        input_mock = MagicMock()
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("builtins.input", input_mock):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=False)

        self.assertEqual(code, 0)
        input_mock.assert_not_called()
        exec_mock.assert_called_once()

    # E2E-05
    def test_level1_with_yes_flag_skips_confirmation(self):
        """level1 + --yes → input() 未呼び出し・実行 → exitcode=0。"""
        steps = [make_step(1, danger_level=1)]
        input_mock = MagicMock()
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("builtins.input", input_mock):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=True)

        self.assertEqual(code, 0)
        input_mock.assert_not_called()
        exec_mock.assert_called_once()

    # E2E-06
    def test_level1_user_confirms_y(self):
        """level1 + --yes なし + ユーザー y → 実行 → exitcode=0。"""
        steps = [make_step(1, danger_level=1)]
        code  = self._run(steps, yes=False, user_input="y")
        self.assertEqual(code, 0)

    # E2E-07
    def test_level1_user_cancels(self):
        """level1 + --yes なし + ユーザー n → キャンセル → exitcode=0。"""
        steps = [make_step(1, danger_level=1)]
        code  = self._run(steps, yes=False, user_input="n")
        self.assertEqual(code, 0)

    # E2E-08
    def test_level2_with_yes_still_requires_confirmation_y(self):
        """level2 + --yes フラグでも input() が呼ばれる → y → exitcode=0。"""
        steps = [make_step(1, danger_level=2)]
        input_mock = MagicMock(return_value="y")
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("builtins.input", input_mock):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=True)

        self.assertEqual(code, 0)
        input_mock.assert_called_once()  # --yes でもスキップ不可
        exec_mock.assert_called_once()

    # E2E-09
    def test_level2_with_yes_user_cancels(self):
        """level2 + --yes フラグ + ユーザー n → キャンセル → exitcode=0。"""
        steps = [make_step(1, danger_level=2)]
        input_mock = MagicMock(return_value="n")
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("builtins.input", input_mock):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=True)

        self.assertEqual(code, 0)
        exec_mock.assert_not_called()

    # E2E-10
    def test_step_failure_aborts(self):
        """ステップが失敗 → 後続ステップは実行されない → exitcode=1。"""
        steps = [
            make_step(1, danger_level=0),
            make_step(2, danger_level=0),
        ]
        plan  = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=mock_execute_step_fail)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=False)

        self.assertEqual(code, 1)
        # ステップ1で失敗 → ステップ2は呼ばれない
        self.assertEqual(exec_mock.call_count, 1)

    # E2E-11
    def test_planner_error_returns_1(self):
        """PlannerError が発生したら exitcode=1。"""
        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.AuditLogger", return_value=_make_logger_mock()), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.side_effect = PlannerError("接続失敗")
            code = main.run("テスト", dry_run=False)

        self.assertEqual(code, 1)


class TestE2EPrevHandoff(unittest.TestCase):
    """$prev / capture_output の統合テスト（main.run() を通す）。"""

    def _run(self, steps: list[dict], exec_side_effect=None, yes: bool = True) -> tuple[int, MagicMock]:
        plan         = make_plan(steps)
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)
        exec_mock    = MagicMock(side_effect=exec_side_effect or mock_execute_step)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", exec_mock), \
             patch("builtins.input", return_value="y"), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=yes)

        return code, exec_mock

    # E2E-12
    def test_prev_single_file(self):
        """$prev: 前ステップが1ファイルを返す → src に展開されて実行 → exitcode=0。"""
        steps = [
            make_step(1, tool="shell", capture_output=True,
                      command="find ~/Downloads -name '*.pdf'",
                      mock_output="/home/john/Downloads/report.pdf"),
            make_step(2, tool="filesystem", action="move",
                      src="$prev", dst="~/Documents/",
                      danger_level=1),
        ]

        def exec_side(step, timeout=30):
            return (True, step.get("_mock_output", ""), None)

        code, exec_mock = self._run(steps, exec_side_effect=exec_side, yes=True)

        self.assertEqual(code, 0)
        self.assertEqual(exec_mock.call_count, 2)
        # ステップ2 の src が $prev から実際のパスに置換されていること
        step2_call = exec_mock.call_args_list[1]
        actual_step = step2_call[0][0]
        self.assertEqual(actual_step["src"], "/home/john/Downloads/report.pdf")

    # E2E-13
    def test_prev_multiple_files(self):
        """$prev: 前ステップが複数ファイルを返す → 1件ずつ execute_step を呼ぶ → exitcode=0。"""
        multi_output = "/home/john/Downloads/a.pdf\n/home/john/Downloads/b.pdf\n/home/john/Downloads/c.pdf"
        steps = [
            make_step(1, tool="shell", capture_output=True,
                      command="find ~/Downloads -name '*.pdf'",
                      mock_output=multi_output),
            make_step(2, tool="filesystem", action="move",
                      src="$prev", dst="~/Documents/",
                      danger_level=1),
        ]

        def exec_side(step, timeout=30):
            return (True, step.get("_mock_output", ""), None)

        code, exec_mock = self._run(steps, exec_side_effect=exec_side, yes=True)

        self.assertEqual(code, 0)
        # ステップ1（1回） + ステップ2を3ファイル分（3回） = 計4回
        self.assertEqual(exec_mock.call_count, 4)
        # ステップ2の各呼び出しで src が個別ファイルになっているか
        srcs = [c[0][0]["src"] for c in exec_mock.call_args_list[1:]]
        self.assertEqual(srcs, [
            "/home/john/Downloads/a.pdf",
            "/home/john/Downloads/b.pdf",
            "/home/john/Downloads/c.pdf",
        ])

    # E2E-14
    def test_prev_none_raises_error(self):
        """$prev が参照されたが前ステップに capture_output がない → exitcode=1。"""
        steps = [
            # capture_output なし
            make_step(1, tool="shell", command="echo hello", mock_output="hello"),
            make_step(2, tool="filesystem", action="move",
                      src="$prev", dst="~/Documents/",
                      danger_level=1),
        ]

        code, _ = self._run(steps, yes=True)
        self.assertEqual(code, 1)

    # E2E-15
    def test_prev_empty_output_skips(self):
        """$prev の出力が空 → ステップをスキップして正常終了 → exitcode=0。"""
        steps = [
            make_step(1, tool="shell", capture_output=True,
                      command="find ~/Downloads -name '*.pdf'",
                      mock_output=""),   # 0件
            make_step(2, tool="filesystem", action="move",
                      src="$prev", dst="~/Documents/",
                      danger_level=1),
        ]

        def exec_side(step, timeout=30):
            return (True, step.get("_mock_output", ""), None)

        code, exec_mock = self._run(steps, exec_side_effect=exec_side, yes=True)

        self.assertEqual(code, 0)
        # ステップ1のみ実行、ステップ2はスキップ
        self.assertEqual(exec_mock.call_count, 1)


class TestE2ELogging(unittest.TestCase):
    """Audit Logger が適切に呼ばれているかを検証する。"""

    # E2E-L01
    def test_logger_called_on_success(self):
        """成功時: task_start / step / task_end が正しく呼ばれる。"""
        steps = [make_step(1, danger_level=0, mock_output="done")]
        plan  = make_plan(steps, task_summary="ログテスト")
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", side_effect=mock_execute_step):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=True)

        self.assertEqual(code, 0)
        logger_mock.append_task_start.assert_called_once_with("ログテスト")
        logger_mock.append_step.assert_called_once()
        logger_mock.append_task_end.assert_called_once_with("ログテスト", "completed")

    # E2E-L02
    def test_logger_called_on_step_failure(self):
        """失敗時: task_end が "aborted" で呼ばれる。"""
        steps = [make_step(1, danger_level=0)]
        plan  = make_plan(steps, task_summary="失敗テスト")
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", side_effect=mock_execute_step_fail), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=True)

        self.assertEqual(code, 1)
        logger_mock.append_task_end.assert_called_once_with("失敗テスト", "aborted")

    # E2E-L03
    def test_logger_called_on_blocked(self):
        """blocked 時: append_step(status="aborted") と task_end("aborted") が呼ばれる。"""
        steps = [
            {**make_step(1, danger_level=3), "_blocked": True},
        ]
        plan  = make_plan(steps, task_summary="ブロックテスト")
        logger_mock  = _make_logger_mock()
        checker_mock = _make_safety_mock(steps)

        with patch("main.TaskPlanner") as MockPlanner, \
             patch("main.SafetyChecker", return_value=checker_mock), \
             patch("main.AuditLogger", return_value=logger_mock), \
             patch("main.execute_step", side_effect=mock_execute_step), \
             patch("sys.stderr", new_callable=StringIO):

            MockPlanner.return_value.plan.return_value = plan
            code = main.run("テスト", dry_run=False, yes=True)

        self.assertEqual(code, 1)
        # append_step が "aborted" で呼ばれたか
        call_kwargs = logger_mock.append_step.call_args[1]
        self.assertEqual(call_kwargs["status"], "aborted")
        logger_mock.append_task_end.assert_called_once_with("ブロックテスト", "aborted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
