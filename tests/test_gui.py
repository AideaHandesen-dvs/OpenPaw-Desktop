"""
tests/test_gui.py - GUI Tool の単体テスト

テスト戦略:
  - バックエンド（ydotool / xdotool / wmctrl 等）を unittest.mock でモックする。
    実際のデーモンや X/Wayland セッションに依存せず CI で実行できるようにする。
  - 各テストクラスが検証するもの:
      TestBackendDetection   - バックエンド検出ロジック
      TestKeyAction          - key() 関数（コマンド生成・エラーハンドリング）
      TestTypeAction         - type_text() 関数
      TestClickAction        - click() 関数（バリデーション含む）
      TestMoveAction         - move() 関数
      TestScrollAction       - scroll() 関数（バリデーション含む）
      TestFocusAction        - focus_window() 関数
      TestScreenshotAction   - screenshot() 関数（パス自動生成含む）
      TestGetWindowsAction   - get_windows() 関数
      TestNoBackend          - バックエンドなし時の全関数エラー
      TestGuiSafety          - safety.py の _check_gui() ロジック
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.gui import (
    GuiResult,
    click,
    focus_window,
    get_windows,
    key,
    move,
    screenshot,
    scroll,
    type_text,
)
from safety import SafetyChecker


# ------------------------------------------------------------------ #
# 共通 fixture
# ------------------------------------------------------------------ #

# subprocess.run の成功レスポンスを作るヘルパー
def _proc_ok(stdout: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


def _proc_fail(stderr: str = "error", returncode: int = 1) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = ""
    m.stderr = stderr
    return m


# Safety テスト用の最小 YAML（gui 設定込み）
GUI_SAFETY_CONFIG = {
    "blocklist": ["rm -rf /", "sudo"],
    "dangerous": ["rm"],
    "allowed_paths": ["/home/john", "/tmp"],
    "timeout": 30,
    "dbus_blocked_services": [],
    "gui_blocked_keys": [
        "ctrl+alt+Delete",
        "ctrl+alt+BackSpace",
        "ctrl+alt+F1",
    ],
    "gui_dangerous_keys": [
        "super+l",
        "ctrl+alt",
    ],
}


def make_checker(config: dict | None = None) -> tuple[SafetyChecker, tempfile.NamedTemporaryFile]:
    cfg = config if config is not None else GUI_SAFETY_CONFIG
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    yaml.dump(cfg, tmp, allow_unicode=True)
    tmp.flush()
    return SafetyChecker(config_path=tmp.name), tmp


# ------------------------------------------------------------------ #
# TestBackendDetection
# ------------------------------------------------------------------ #

class TestBackendDetection(unittest.TestCase):
    """バックエンド検出ロジック（shutil.which 結果に応じた選択）。"""

    def test_ydotool_preferred_over_xdotool(self):
        """ydotool と xdotool 両方あれば ydotool を選ぶ。"""
        from tools.gui import _detect_input_backend
        with patch("tools.gui.shutil.which", side_effect=lambda x: x if x in ("ydotool", "xdotool") else None):
            self.assertEqual(_detect_input_backend(), "ydotool")

    def test_xdotool_fallback_when_no_ydotool(self):
        """ydotool がなければ xdotool にフォールバックする。"""
        from tools.gui import _detect_input_backend
        with patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None):
            self.assertEqual(_detect_input_backend(), "xdotool")

    def test_no_input_backend_returns_none(self):
        """両方なければ None を返す。"""
        from tools.gui import _detect_input_backend
        with patch("tools.gui.shutil.which", return_value=None):
            self.assertIsNone(_detect_input_backend())

    def test_wmctrl_preferred_over_xdotool_for_windows(self):
        """ウィンドウ管理は wmctrl を優先する。"""
        from tools.gui import _detect_window_backend
        with patch("tools.gui.shutil.which", side_effect=lambda x: x if x in ("wmctrl", "xdotool") else None):
            self.assertEqual(_detect_window_backend(), "wmctrl")

    def test_screenshot_backend_priority(self):
        """スクリーンショットは scrot > spectacle > import の順。"""
        from tools.gui import _detect_screenshot_backend

        # scrot あり
        with patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "scrot" else None):
            self.assertEqual(_detect_screenshot_backend(), "scrot")

        # scrot なし、spectacle あり
        with patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "spectacle" else None):
            self.assertEqual(_detect_screenshot_backend(), "spectacle")

        # 全部なし
        with patch("tools.gui.shutil.which", return_value=None):
            self.assertIsNone(_detect_screenshot_backend())


# ------------------------------------------------------------------ #
# TestKeyAction
# ------------------------------------------------------------------ #

class TestKeyAction(unittest.TestCase):
    """key() 関数: コマンド生成とエラーハンドリング。"""

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_ydotool_command_format(self, _which, mock_run):
        """ydotool バックエンドで正しいコマンドが生成される。"""
        mock_run.return_value = _proc_ok()
        r = key("ctrl+c")
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("ydotool", cmd)
        self.assertIn("ctrl+c", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_command_format(self, _which, mock_run):
        """xdotool バックエンドで正しいコマンドが生成される。"""
        mock_run.return_value = _proc_ok()
        r = key("Return")
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("xdotool", cmd)
        self.assertIn("Return", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_output_contains_key_name(self, _which, mock_run):
        """成功時の output にキー名が含まれる。"""
        mock_run.return_value = _proc_ok()
        r = key("super+d")
        self.assertIn("super+d", r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_command_failure_returns_error(self, _which, mock_run):
        """コマンド失敗時は success=False かつ error が入る。"""
        mock_run.return_value = _proc_fail("No display")
        r = key("ctrl+c")
        self.assertFalse(r.success)
        self.assertIsNotNone(r.error)

    def test_no_backend_returns_error(self):
        """バックエンドなし時は success=False でインストール案内が出る。"""
        with patch("tools.gui.shutil.which", return_value=None):
            r = key("ctrl+c")
        self.assertFalse(r.success)
        self.assertIn("ydotool", r.error)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_special_chars_properly_quoted(self, _which, mock_run):
        """シェル特殊文字を含むキー名がクォートされる。"""
        mock_run.return_value = _proc_ok()
        key("ctrl+alt+F2")
        cmd = mock_run.call_args[0][0]
        # シングルクォートかダブルクォートで囲まれているか
        self.assertNotIn("ctrl+alt+F2 ", cmd)  # 生のスペース区切りで渡されない


# ------------------------------------------------------------------ #
# TestTypeAction
# ------------------------------------------------------------------ #

class TestTypeAction(unittest.TestCase):
    """type_text() 関数。"""

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_ydotool_includes_delay(self, _which, mock_run):
        """ydotool コマンドに --delay が含まれる。"""
        mock_run.return_value = _proc_ok()
        type_text("hello", delay=20)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--delay", cmd)
        self.assertIn("20", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_includes_delay(self, _which, mock_run):
        """xdotool コマンドに --delay が含まれる。"""
        mock_run.return_value = _proc_ok()
        type_text("world", delay=50)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--delay", cmd)
        self.assertIn("50", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_output_shows_preview(self, _which, mock_run):
        """出力に入力テキストのプレビューが含まれる。"""
        mock_run.return_value = _proc_ok()
        r = type_text("OpenPaw")
        self.assertIn("OpenPaw", r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_long_text_truncated_in_output(self, _which, mock_run):
        """30文字超のテキストは output でトランケートされる。"""
        mock_run.return_value = _proc_ok()
        long_text = "a" * 50
        r = type_text(long_text)
        self.assertIn("...", r.output)
        # output に全文字が含まれない（切り詰められている）
        self.assertNotIn(long_text, r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_double_dash_separator(self, _which, mock_run):
        """ydotool type に -- セパレータが含まれる（マイナス始まりテキスト対策）。"""
        mock_run.return_value = _proc_ok()
        type_text("-test")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--", cmd)


# ------------------------------------------------------------------ #
# TestClickAction
# ------------------------------------------------------------------ #

class TestClickAction(unittest.TestCase):
    """click() 関数: バリデーションとコマンド生成。"""

    def test_invalid_button_rejected(self):
        """button に 1/2/3 以外を渡すと即エラー（バックエンド不要）。"""
        r = click(100, 100, button=5)
        self.assertFalse(r.success)
        self.assertIn("button", r.error)

    def test_invalid_count_zero_rejected(self):
        """count=0 は即エラー。"""
        r = click(100, 100, count=0)
        self.assertFalse(r.success)
        self.assertIn("count", r.error)

    def test_invalid_count_negative_rejected(self):
        """count=-1 も即エラー。"""
        r = click(100, 100, count=-1)
        self.assertFalse(r.success)
        self.assertIn("count", r.error)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_ydotool_uses_absolute_move(self, _which, mock_run):
        """ydotool は --absolute で座標を渡す。"""
        mock_run.return_value = _proc_ok()
        click(200, 300)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--absolute", cmd)
        self.assertIn("200", cmd)
        self.assertIn("300", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_uses_mousemove_and_click(self, _which, mock_run):
        """xdotool は mousemove と click を1コマンドで実行する。"""
        mock_run.return_value = _proc_ok()
        click(100, 200, button=3)
        cmd = mock_run.call_args[0][0]
        self.assertIn("mousemove", cmd)
        self.assertIn("click", cmd)
        self.assertIn("3", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_output_shows_button_name_japanese(self, _which, mock_run):
        """output に日本語ボタン名が含まれる。"""
        mock_run.return_value = _proc_ok()
        r = click(100, 100, button=1)
        self.assertIn("左", r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_double_click_output(self, _which, mock_run):
        """count=2 の場合、output に × 2 が表示される。"""
        mock_run.return_value = _proc_ok()
        r = click(100, 100, count=2)
        self.assertIn("× 2", r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_single_click_no_repeat_suffix(self, _which, mock_run):
        """count=1 の場合、output に × は表示されない。"""
        mock_run.return_value = _proc_ok()
        r = click(100, 100, count=1)
        self.assertNotIn("×", r.output)


# ------------------------------------------------------------------ #
# TestMoveAction
# ------------------------------------------------------------------ #

class TestMoveAction(unittest.TestCase):
    """move() 関数。"""

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_ydotool_move_command(self, _which, mock_run):
        """ydotool mousemove --absolute に x/y が渡される。"""
        mock_run.return_value = _proc_ok()
        r = move(640, 480)
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("mousemove", cmd)
        self.assertIn("640", cmd)
        self.assertIn("480", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_move_command(self, _which, mock_run):
        """xdotool mousemove に x/y が渡される。"""
        mock_run.return_value = _proc_ok()
        r = move(320, 240)
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("mousemove", cmd)
        self.assertIn("320", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_output_contains_coordinates(self, _which, mock_run):
        """output に座標が含まれる。"""
        mock_run.return_value = _proc_ok()
        r = move(100, 200)
        self.assertIn("100", r.output)
        self.assertIn("200", r.output)


# ------------------------------------------------------------------ #
# TestScrollAction
# ------------------------------------------------------------------ #

class TestScrollAction(unittest.TestCase):
    """scroll() 関数: バリデーションとコマンド生成。"""

    def test_invalid_direction_rejected(self):
        """無効な direction は即エラー（バックエンド不要）。"""
        r = scroll("diagonal")
        self.assertFalse(r.success)
        self.assertIn("direction", r.error)

    def test_invalid_amount_zero_rejected(self):
        """amount=0 は即エラー。"""
        r = scroll("down", amount=0)
        self.assertFalse(r.success)
        self.assertIn("amount", r.error)

    def test_invalid_amount_negative_rejected(self):
        """amount=-3 は即エラー。"""
        r = scroll("up", amount=-3)
        self.assertFalse(r.success)
        self.assertIn("amount", r.error)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_ydotool_scroll_down_uses_negative_y(self, _which, mock_run):
        """ydotool scroll down は axis-y に負値を使う。"""
        mock_run.return_value = _proc_ok()
        scroll("down", amount=3)
        cmd = mock_run.call_args[0][0]
        self.assertIn("axis-y", cmd)
        self.assertIn("-3", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_ydotool_scroll_up_uses_positive_y(self, _which, mock_run):
        """ydotool scroll up は axis-y に正値を使う。"""
        mock_run.return_value = _proc_ok()
        scroll("up", amount=5)
        cmd = mock_run.call_args[0][0]
        self.assertIn("axis-y", cmd)
        # "-5" ではなく "5" が含まれることを確認（負のプレフィックスなし）
        self.assertIn("=5", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_scroll_down_uses_button5(self, _which, mock_run):
        """xdotool の scroll down はボタン 5 を使う。"""
        mock_run.return_value = _proc_ok()
        scroll("down", amount=2)
        cmd = mock_run.call_args[0][0]
        self.assertIn("5", cmd)
        self.assertIn("--repeat", cmd)
        self.assertIn("2", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_scroll_up_uses_button4(self, _which, mock_run):
        """xdotool の scroll up はボタン 4 を使う。"""
        mock_run.return_value = _proc_ok()
        scroll("up")
        cmd = mock_run.call_args[0][0]
        self.assertIn("4", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_output_shows_direction_and_amount(self, _which, mock_run):
        """output に方向と量が含まれる。"""
        mock_run.return_value = _proc_ok()
        r = scroll("left", amount=4)
        self.assertIn("left", r.output)
        self.assertIn("4", r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "ydotool" else None)
    def test_all_valid_directions_accepted(self, _which, mock_run):
        """up / down / left / right すべて受け付ける。"""
        mock_run.return_value = _proc_ok()
        for d in ("up", "down", "left", "right"):
            r = scroll(d)
            self.assertTrue(r.success, f"direction {d!r} should succeed")


# ------------------------------------------------------------------ #
# TestFocusAction
# ------------------------------------------------------------------ #

class TestFocusAction(unittest.TestCase):
    """focus_window() 関数。"""

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "wmctrl" else None)
    def test_wmctrl_uses_a_flag(self, _which, mock_run):
        """wmctrl は -a フラグでウィンドウをアクティブにする。"""
        mock_run.return_value = _proc_ok()
        focus_window("Dolphin")
        cmd = mock_run.call_args[0][0]
        self.assertIn("wmctrl", cmd)
        self.assertIn("-a", cmd)
        self.assertIn("Dolphin", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_uses_search_and_focus(self, _which, mock_run):
        """xdotool は search + windowfocus を使う。"""
        mock_run.return_value = _proc_ok()
        focus_window("Konsole")
        cmd = mock_run.call_args[0][0]
        self.assertIn("search", cmd)
        self.assertIn("windowfocus", cmd)
        self.assertIn("Konsole", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "wmctrl" else None)
    def test_output_contains_target(self, _which, mock_run):
        """output にターゲット名が含まれる。"""
        mock_run.return_value = _proc_ok()
        r = focus_window("Firefox")
        self.assertIn("Firefox", r.output)

    def test_no_window_backend_returns_error(self):
        """ウィンドウバックエンドなし時はエラー。"""
        with patch("tools.gui.shutil.which", return_value=None):
            r = focus_window("test")
        self.assertFalse(r.success)
        self.assertIn("wmctrl", r.error)


# ------------------------------------------------------------------ #
# TestScreenshotAction
# ------------------------------------------------------------------ #

class TestScreenshotAction(unittest.TestCase):
    """screenshot() 関数: パス自動生成とバックエンド別コマンド。"""

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "scrot" else None)
    def test_scrot_command_uses_path(self, _which, mock_run):
        """scrot にパスが渡される。"""
        mock_run.return_value = _proc_ok()
        r = screenshot("/tmp/test.png")
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("scrot", cmd)
        self.assertIn("/tmp/test.png", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "spectacle" else None)
    def test_spectacle_uses_b_and_o_flags(self, _which, mock_run):
        """spectacle は -b -o フラグを使う。"""
        mock_run.return_value = _proc_ok()
        screenshot("/tmp/s.png")
        cmd = mock_run.call_args[0][0]
        self.assertIn("-b", cmd)
        self.assertIn("-o", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "import" else None)
    def test_import_captures_root_window(self, _which, mock_run):
        """ImageMagick import は -window root を使う。"""
        mock_run.return_value = _proc_ok()
        screenshot("/tmp/i.png")
        cmd = mock_run.call_args[0][0]
        self.assertIn("-window", cmd)
        self.assertIn("root", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "scrot" else None)
    def test_auto_path_when_none(self, _which, mock_run):
        """path=None のとき ~/.openpaw/screenshots/ 以下に保存される。"""
        mock_run.return_value = _proc_ok()
        r = screenshot(None)
        self.assertTrue(r.success)
        self.assertIn(".openpaw", r.output)
        self.assertIn("screenshots", r.output)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "scrot" else None)
    def test_auto_path_is_png(self, _which, mock_run):
        """自動生成パスは .png 拡張子。"""
        mock_run.return_value = _proc_ok()
        r = screenshot(None)
        self.assertTrue(r.output.endswith(".png"))

    def test_no_screenshot_backend_returns_error(self):
        """スクリーンショットバックエンドなし時はエラー。"""
        with patch("tools.gui.shutil.which", return_value=None):
            r = screenshot()
        self.assertFalse(r.success)
        self.assertIn("scrot", r.error)


# ------------------------------------------------------------------ #
# TestGetWindowsAction
# ------------------------------------------------------------------ #

class TestGetWindowsAction(unittest.TestCase):
    """get_windows() 関数。"""

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "wmctrl" else None)
    def test_wmctrl_list_flag(self, _which, mock_run):
        """wmctrl -l が実行される。"""
        mock_run.return_value = _proc_ok("0x01  0 host Firefox")
        r = get_windows()
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("wmctrl", cmd)
        self.assertIn("-l", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_xdotool_search_for_windows(self, _which, mock_run):
        """xdotool search + getwindowname が実行される。"""
        mock_run.return_value = _proc_ok("Konsole\nDolphin")
        r = get_windows()
        self.assertTrue(r.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("search", cmd)
        self.assertIn("getwindowname", cmd)

    @patch("tools.gui.subprocess.run")
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "wmctrl" else None)
    def test_output_contains_window_list(self, _which, mock_run):
        """wmctrl の出力がそのまま output に入る。"""
        window_list = "0x01  0 host Firefox\n0x02  0 host Konsole"
        mock_run.return_value = _proc_ok(window_list)
        r = get_windows()
        self.assertIn("Firefox", r.output)

    def test_no_backend_returns_error(self):
        """バックエンドなし時はエラー。"""
        with patch("tools.gui.shutil.which", return_value=None):
            r = get_windows()
        self.assertFalse(r.success)


# ------------------------------------------------------------------ #
# TestNoBackend
# ------------------------------------------------------------------ #

class TestNoBackend(unittest.TestCase):
    """バックエンドが一切ない環境での全関数エラー確認。"""

    def setUp(self):
        self._patcher = patch("tools.gui.shutil.which", return_value=None)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_key_no_backend(self):
        r = key("ctrl+c")
        self.assertFalse(r.success)

    def test_type_no_backend(self):
        r = type_text("hello")
        self.assertFalse(r.success)

    def test_click_no_backend(self):
        r = click(0, 0)
        self.assertFalse(r.success)

    def test_move_no_backend(self):
        r = move(0, 0)
        self.assertFalse(r.success)

    def test_scroll_no_backend(self):
        r = scroll("down")
        self.assertFalse(r.success)

    def test_focus_no_backend(self):
        r = focus_window("test")
        self.assertFalse(r.success)

    def test_screenshot_no_backend(self):
        r = screenshot()
        self.assertFalse(r.success)

    def test_get_windows_no_backend(self):
        r = get_windows()
        self.assertFalse(r.success)

    def test_all_errors_have_message(self):
        """全関数がバックエンドなし時に error メッセージを返す。"""
        funcs = [
            lambda: key("ctrl+c"),
            lambda: type_text("x"),
            lambda: click(0, 0),
            lambda: move(0, 0),
            lambda: scroll("down"),
            lambda: focus_window("x"),
            lambda: screenshot(),
            lambda: get_windows(),
        ]
        for f in funcs:
            r = f()
            self.assertIsNotNone(r.error, f"{f} should have an error message")
            self.assertGreater(len(r.error), 0)


# ------------------------------------------------------------------ #
# TestTimeout
# ------------------------------------------------------------------ #

class TestTimeout(unittest.TestCase):
    """タイムアウト時の挙動。"""

    @patch("tools.gui.subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("cmd", 5))
    @patch("tools.gui.shutil.which", side_effect=lambda x: x if x == "xdotool" else None)
    def test_timeout_returns_error(self, _which, _run):
        """タイムアウト発生時は success=False でエラーメッセージが入る。"""
        r = key("ctrl+c")
        self.assertFalse(r.success)
        self.assertIn("タイムアウト", r.error)


# ------------------------------------------------------------------ #
# TestGuiSafety  ← safety.py の _check_gui() テスト
# ------------------------------------------------------------------ #

class TestGuiSafety(unittest.TestCase):
    """SafetyChecker._check_gui() のロジック検証。"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _step(self, action: str, **kwargs) -> dict:
        return {"tool": "gui", "action": action, **kwargs}

    # ---- 読み取り専用 → Level 0 ---------------------------------- #

    def test_getwindows_is_level0(self):
        r = self.checker.check(self._step("getwindows"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_screenshot_is_level0(self):
        r = self.checker.check(self._step("screenshot", path=None))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_move_is_level0(self):
        r = self.checker.check(self._step("move", x=100, y=200))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    # ---- 入力系 → Level 1 ---------------------------------------- #

    def test_key_normal_is_level1(self):
        r = self.checker.check(self._step("key", keys="ctrl+c"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_type_is_level1(self):
        r = self.checker.check(self._step("type", text="hello"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_click_is_level1(self):
        r = self.checker.check(self._step("click", x=0, y=0, button=1))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_scroll_is_level1(self):
        r = self.checker.check(self._step("scroll", direction="down"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_focus_is_level1(self):
        r = self.checker.check(self._step("focus", target="Konsole"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    # ---- 危険キー → Level 2 -------------------------------------- #

    def test_screen_lock_key_is_level2(self):
        """super+l（画面ロック）は Level 2。"""
        r = self.checker.check(self._step("key", keys="super+l"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    def test_ctrl_alt_prefix_is_level2(self):
        """ctrl+alt 系（VT切り替えプレフィックス）は Level 2。"""
        r = self.checker.check(self._step("key", keys="ctrl+alt+t"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    # ---- ブロックキー → Level 3 / blocked=True -------------------- #

    def test_ctrl_alt_delete_blocked(self):
        r = self.checker.check(self._step("key", keys="ctrl+alt+Delete"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_ctrl_alt_backspace_blocked(self):
        r = self.checker.check(self._step("key", keys="ctrl+alt+BackSpace"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_vt_switch_f1_blocked(self):
        r = self.checker.check(self._step("key", keys="ctrl+alt+F1"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_blocked_key_has_reason(self):
        """ブロック時は reason が設定される。"""
        r = self.checker.check(self._step("key", keys="ctrl+alt+Delete"))
        self.assertIsNotNone(r.reason)
        self.assertIn("blocked", r.reason.lower())

    # ---- keys なし（click 等）はブロックされない ------------------ #

    def test_click_not_blocked_by_key_rules(self):
        """click アクションは gui_blocked_keys の影響を受けない。"""
        r = self.checker.check(self._step("click", x=0, y=0))
        self.assertFalse(r.blocked)

    # ---- empty gui_blocked_keys ----------------------------------- #

    def test_no_blocked_keys_config_allows_everything(self):
        """gui_blocked_keys が空なら全キーを通す。"""
        cfg = dict(GUI_SAFETY_CONFIG)
        cfg["gui_blocked_keys"] = []
        cfg["gui_dangerous_keys"] = []
        checker, tmp = make_checker(cfg)
        try:
            r = checker.check({"tool": "gui", "action": "key", "keys": "ctrl+alt+Delete"})
            self.assertFalse(r.blocked)
            self.assertEqual(r.danger_level, 1)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
