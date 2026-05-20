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
            "tool": "shell",
            "description": "Downloadsのファイル一覧を表示",
            "command": "ls -la ~/Downloads",
        }
    ],
})

VALID_FILESYSTEM_PLAN = json.dumps({
    "task_summary": "PDFをDocumentsに移動する",
    "steps": [
        {
            "tool": "shell",
            "description": "PDFを検索",
            "command": "find ~/Downloads -name '*.pdf'",
        },
        {
            "tool": "filesystem",
            "description": "PDFをDocumentsへ移動",
            "action": "move",
            "src": "~/Downloads/*.pdf",
            "dst": "~/Documents/",
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

    def test_step_id_assigned_by_code(self):
        """step_id はコードが連番で付与する（LLMが出力しなくてよい）。"""
        planner = TaskPlanner(client=MockOllamaClient(VALID_FILESYSTEM_PLAN))
        plan = planner.plan("PDFをDocumentsに移動して")
        self.assertEqual(plan.steps[0]["step_id"], 1)
        self.assertEqual(plan.steps[1]["step_id"], 2)

    def test_on_error_default_set_by_code(self):
        """on_error はコードがデフォルト値 'abort' を補完する（LLMが出力しなくてよい）。"""
        planner = TaskPlanner(client=MockOllamaClient(VALID_SHELL_PLAN))
        plan = planner.plan("テスト")
        self.assertEqual(plan.steps[0]["on_error"], "abort")

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
            {"tool": "shell", "description": "x", "command": "ls"}
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
            {"tool": "shell", "description": "x"}
            # command なし
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_filesystem_missing_action_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"tool": "filesystem", "description": "x", "src": "~/file.txt"}
            # action なし
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_filesystem_move_missing_dst_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"tool": "filesystem", "description": "x",
             "action": "move", "src": "~/file.txt"}
            # dst なし
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")

    def test_invalid_tool_raises(self):
        bad = json.dumps({"task_summary": "テスト", "steps": [
            {"tool": "browser", "description": "x"}
        ]})
        with self.assertRaises(PlannerError):
            self._planner(bad).plan("テスト")


class TestParseMethodNames(unittest.TestCase):
    """_parse_method_names のユニットテスト。"""

    def _planner(self) -> TaskPlanner:
        return TaskPlanner(client=MockOllamaClient(VALID_SHELL_PLAN))

    def test_extracts_method_names(self):
        planner = self._planner()
        methods_str = (
            ".toggleDisplay    method    -    -    -\n"
            ".query            method    s    as   -\n"
            ".display          method    -    -    -\n"
        )
        result = planner._parse_method_names(methods_str)
        self.assertEqual(result, {"toggleDisplay", "query", "display"})

    def test_empty_string_returns_empty_set(self):
        planner = self._planner()
        self.assertEqual(planner._parse_method_names(""), set())

    def test_ignores_non_method_lines(self):
        planner = self._planner()
        methods_str = (
            "org.kde.krunner.App    interface    -    -    -\n"
            ".toggleDisplay         method       -    -    -\n"
            ".Running               property     b    -    emits-change\n"
        )
        result = planner._parse_method_names(methods_str)
        self.assertEqual(result, {"toggleDisplay"})


class TestEnforceDbusMethodsBUG006(unittest.TestCase):
    """
    BUG-006: introspect後に存在しないメソッドをdbusで強行する問題の修正テスト。
    _enforce_dbus_methods() の動作を検証する。
    """

    SHELL_FALLBACK_PLAN = json.dumps({
        "task_summary": "shellでKRunner履歴をクリアする",
        "steps": [{
            "tool": "shell",
            "description": "krunnerrcから履歴を削除",
            "command": "sed -i '/history/d' ~/.config/krunnerrc",
        }],
    })

    DBUS_PLAN_WITH_INVALID_METHOD = json.dumps({
        "task_summary": "KRunner履歴をクリアする",
        "steps": [{
            "tool": "dbus",
            "action": "call",
            "service": "org.kde.krunner",
            "object": "/App",
            "interface": "org.kde.krunner.App",
            "method": "CleanHistory",   # 存在しないメソッド
            "args": [],
            "arg_types": [],
            "bus": "session",
            "description": "履歴をクリア",
        }],
    })

    def test_invalid_method_triggers_fallback(self):
        """存在しないメソッドが使われた場合、shellで再プランニングされる。"""
        # _enforce_dbus_methods を直接呼ぶので、
        # generate() の呼び出しは1回（shellフォールバック用）のみ。
        class SequentialMock:
            def __init__(self, response: str):
                self.calls: list[str] = []
                self._response = response
            def generate(self, prompt: str) -> str:
                self.calls.append(prompt)
                return self._response

        mock = SequentialMock(self.SHELL_FALLBACK_PLAN)
        planner = TaskPlanner(client=mock)

        valid_methods = {"toggleDisplay", "query", "display"}  # CleanHistoryは含まない
        dbus_plan = planner._parse(self.DBUS_PLAN_WITH_INVALID_METHOD)
        result = planner._enforce_dbus_methods("KRunnerの履歴を消して", dbus_plan, valid_methods)

        # shellにフォールバックされていること
        self.assertEqual(result.steps[0]["tool"], "shell")
        # 再プランニングのプロンプトに無効メソッド名が含まれること
        self.assertIn("CleanHistory", mock.calls[0])

    def test_valid_method_passes_through(self):
        """実在するメソッドが使われた場合、プランはそのまま返る。"""
        DBUS_PLAN_WITH_VALID_METHOD = json.dumps({
            "task_summary": "KRunnerを開閉する",
            "steps": [{
                "tool": "dbus",
                "action": "call",
                "service": "org.kde.krunner",
                "object": "/App",
                "interface": "org.kde.krunner.App",
                "method": "toggleDisplay",  # 実在するメソッド
                "args": [],
                "arg_types": [],
                "bus": "session",
                "description": "KRunnerを開閉する",
            }],
        })
        mock = MockOllamaClient(VALID_SHELL_PLAN)  # フォールバックは呼ばれないはず
        planner = TaskPlanner(client=mock)

        valid_methods = {"toggleDisplay", "query"}
        dbus_plan = planner._parse(DBUS_PLAN_WITH_VALID_METHOD)
        result = planner._enforce_dbus_methods("KRunnerを開閉して", dbus_plan, valid_methods)

        # dbusのままであること（shellに変換されていない）
        self.assertEqual(result.steps[0]["tool"], "dbus")
        self.assertEqual(result.steps[0]["method"], "toggleDisplay")
        # generate() は呼ばれていないこと（フォールバック不要）
        self.assertEqual(mock.called_with, [])

    def test_non_call_dbus_action_not_affected(self):
        """dbus/call 以外のアクション（introspect等）は検証対象外。"""
        INTROSPECT_PLAN = json.dumps({
            "task_summary": "introspect",
            "steps": [{
                "tool": "dbus",
                "action": "introspect",  # call ではない
                "service": "org.kde.krunner",
                "object": "/App",
                "interface": "org.kde.krunner.App",
                "description": "introspect",
            }],
        })
        mock = MockOllamaClient(VALID_SHELL_PLAN)
        planner = TaskPlanner(client=mock)

        valid_methods = {"toggleDisplay"}
        plan = planner._parse(INTROSPECT_PLAN)
        result = planner._enforce_dbus_methods("introspect", plan, valid_methods)

        # introspect ステップはそのまま通過すること
        self.assertEqual(result.steps[0]["action"], "introspect")
        self.assertEqual(mock.called_with, [])


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

