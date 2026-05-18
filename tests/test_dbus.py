"""
tests/test_dbus.py - D-Bus Tool および Safety Checker (dbus拡張) の単体テスト

構成:
  [A] tools/dbus.py 単体テスト
      TestDbusResult            - DbusResult dataclass
      TestDetectBackend         - バックエンド検出（busctl > qdbus 優先順）
      TestIsSimpleSignature     - 型シグネチャ判定ヘルパー
      TestIntrospectMethodSig   - 内部 introspect ヘルパー（シグネチャ取得・パース）
      TestDbusCall              - call()（auto-introspect ロジックを含む）
      TestDbusGetProperty       - get_property()
      TestDbusSetProperty       - set_property()（busctl 必須制約）
      TestDbusListServices      - list_services()
      TestDbusIntrospect        - 公開 introspect() アクション

  [B] safety.py dbus 拡張
      TestSafetyCheckerDbus     - _check_dbus() の全分岐

  [C] 統合テスト（D-Bus セッションがない場合は自動スキップ）
      TestDbusIntegration

モック戦略:
  - 環境依存を排除するため tools.dbus._run_cmd と tools.dbus._detect_backend を mock
  - _detect_backend は各テストで固定値を返させ、バックエンド分岐を明示的にテスト
  - _run_cmd は引数の正しさ（cmd リスト）と戻り値の処理を検証
  - auto-introspect のテストは _introspect_method_signature を mock して分離
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.dbus import (
    DbusResult,
    _detect_backend,
    _introspect_method_signature,
    _is_simple_signature,
    call,
    get_property,
    introspect,
    list_services,
    set_property,
)
from safety import SafetyChecker


# ------------------------------------------------------------------ #
# Safety Checker テスト用 fixture
# ------------------------------------------------------------------ #

MINIMAL_DBUS_CONFIG = {
    "blocklist": ["rm -rf /", "sudo"],
    "dangerous": ["rm", "chmod"],
    "allowed_paths": ["/home/john", "/tmp"],
    "timeout": 30,
    "dbus_blocked_services": [
        "org.freedesktop.systemd1",
        "org.freedesktop.login1",
    ],
}


def make_checker(config=None):
    """一時設定ファイルから SafetyChecker を生成する。"""
    cfg = config if config is not None else MINIMAL_DBUS_CONFIG
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    yaml.dump(cfg, tmp, allow_unicode=True)
    tmp.flush()
    return SafetyChecker(config_path=tmp.name), tmp


# ------------------------------------------------------------------ #
# [A-1] TestDbusResult
# ------------------------------------------------------------------ #

class TestDbusResult(unittest.TestCase):
    """DbusResult dataclass が正しい属性を持つか確認する。"""

    def test_success_result(self):
        r = DbusResult(success=True, output="done")
        self.assertTrue(r.success)
        self.assertEqual(r.output, "done")
        self.assertIsNone(r.error)

    def test_failure_result(self):
        r = DbusResult(success=False, output="", error="something went wrong")
        self.assertFalse(r.success)
        self.assertEqual(r.output, "")
        self.assertEqual(r.error, "something went wrong")

    def test_error_defaults_to_none(self):
        r = DbusResult(success=True, output="ok")
        self.assertIsNone(r.error)


# ------------------------------------------------------------------ #
# [A-2] TestDetectBackend
# ------------------------------------------------------------------ #

class TestDetectBackend(unittest.TestCase):
    """_detect_backend() のバックエンド優先順序とエラーを確認する。"""

    @patch("shutil.which")
    def test_prefers_busctl_over_qdbus(self, mock_which):
        """busctl と qdbus が両方あれば busctl を優先する。"""
        mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x in ("busctl", "qdbus") else None
        self.assertEqual(_detect_backend(), "busctl")

    @patch("shutil.which")
    def test_falls_back_to_qdbus(self, mock_which):
        """busctl がなければ qdbus を使う。"""
        mock_which.side_effect = lambda x: "/usr/bin/qdbus" if x == "qdbus" else None
        self.assertEqual(_detect_backend(), "qdbus")

    @patch("shutil.which", return_value=None)
    def test_raises_when_neither_available(self, _):
        """どちらもなければ RuntimeError を raise する。"""
        with self.assertRaises(RuntimeError) as ctx:
            _detect_backend()
        self.assertIn("バックエンド", str(ctx.exception))


# ------------------------------------------------------------------ #
# [A-3] TestIsSimpleSignature
# ------------------------------------------------------------------ #

class TestIsSimpleSignature(unittest.TestCase):
    """_is_simple_signature() の判定ロジックを確認する。"""

    def test_single_string_is_simple(self):
        self.assertTrue(_is_simple_signature("s"))

    def test_multiple_simple_types(self):
        self.assertTrue(_is_simple_signature("siud"))

    def test_all_simple_type_codes(self):
        # y b n q i u x t d s h がすべて単純型
        self.assertTrue(_is_simple_signature("ybnqiuxtdsh"))

    def test_empty_string_is_not_simple(self):
        """空文字はシグネチャなし（引数なし）= False。"""
        self.assertFalse(_is_simple_signature(""))

    def test_array_prefix_is_not_simple(self):
        self.assertFalse(_is_simple_signature("as"))

    def test_dict_is_not_simple(self):
        self.assertFalse(_is_simple_signature("a{sv}"))

    def test_variant_is_not_simple(self):
        self.assertFalse(_is_simple_signature("v"))

    def test_struct_is_not_simple(self):
        self.assertFalse(_is_simple_signature("(su)"))

    def test_mixed_simple_and_array_is_not_simple(self):
        self.assertFalse(_is_simple_signature("sas"))


# ------------------------------------------------------------------ #
# [A-4] TestIntrospectMethodSig
# ------------------------------------------------------------------ #

# busctl introspect の出力サンプル（org.kde.krunner.App）
_INTROSPECT_OUTPUT = """\
NAME                                   TYPE      SIGNATURE RESULT/VALUE FLAGS
org.kde.krunner.App                    interface -         -            -
.CleanHistory                          method    -         -            -
.Query                                 method    s         as           -
.Match                                 method    s         -            -
.RunMatch                              method    ss        -            -
org.freedesktop.DBus.Introspectable    interface -         -            -
.Introspect                            method    -         s            -
"""


class TestIntrospectMethodSig(unittest.TestCase):
    """_introspect_method_signature() のパースと失敗ケースを確認する。"""

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value=_INTROSPECT_OUTPUT)
    def test_finds_simple_signature(self, _run, _be):
        """単純型シグネチャを正しく取得できる。"""
        sig = _introspect_method_signature(
            "org.kde.krunner", "/App", "org.kde.krunner.App",
            "Query", "session", 30
        )
        self.assertEqual(sig, "s")

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value=_INTROSPECT_OUTPUT)
    def test_finds_multi_arg_signature(self, _run, _be):
        """複数引数のシグネチャを取得できる。"""
        sig = _introspect_method_signature(
            "org.kde.krunner", "/App", "org.kde.krunner.App",
            "RunMatch", "session", 30
        )
        self.assertEqual(sig, "ss")

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value=_INTROSPECT_OUTPUT)
    def test_returns_none_for_no_arg_method(self, _run, _be):
        """引数なしメソッド（"-"）は None を返す。"""
        sig = _introspect_method_signature(
            "org.kde.krunner", "/App", "org.kde.krunner.App",
            "CleanHistory", "session", 30
        )
        self.assertIsNone(sig)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value=_INTROSPECT_OUTPUT)
    def test_returns_none_when_method_not_found(self, _run, _be):
        """存在しないメソッドは None を返す。"""
        sig = _introspect_method_signature(
            "org.kde.krunner", "/App", "org.kde.krunner.App",
            "NoSuchMethod", "session", 30
        )
        self.assertIsNone(sig)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", side_effect=RuntimeError("exit 1"))
    def test_returns_none_on_busctl_error(self, _run, _be):
        """busctl が失敗した場合は None を返す（例外は伝播させない）。"""
        sig = _introspect_method_signature(
            "svc", "/obj", "iface", "Method", "session", 30
        )
        self.assertIsNone(sig)

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    def test_returns_none_with_qdbus_backend(self, _be):
        """qdbus バックエンドでは非対応として None を返す。"""
        sig = _introspect_method_signature(
            "svc", "/obj", "iface", "Method", "session", 30
        )
        self.assertIsNone(sig)


# ------------------------------------------------------------------ #
# [A-5] TestDbusCall
# ------------------------------------------------------------------ #

class TestDbusCall(unittest.TestCase):
    """
    call() 関数のテスト。
    auto-introspect（args あり・arg_types なし）のロジックを含む。
    """

    def _kw(self, **overrides):
        """テスト用のデフォルト引数を返す helper。"""
        base = dict(
            service="org.kde.krunner",
            object_path="/App",
            interface="org.kde.krunner.App",
            method="CleanHistory",
        )
        return {**base, **overrides}

    # ---- 引数なしケース ---- #

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_no_args_busctl_cmd_structure(self, mock_run, _):
        """引数なし: busctl --user call <service> <obj> <iface> <method>。"""
        call(**self._kw())
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "busctl")
        self.assertEqual(cmd[1], "--user")
        self.assertEqual(cmd[2], "call")
        self.assertIn("org.kde.krunner", cmd)
        self.assertIn("CleanHistory", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_system_bus_uses_system_flag(self, mock_run, _):
        """bus='system' のとき --system フラグが使われる。"""
        call(**self._kw(bus="system"))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--system", cmd)
        self.assertNotIn("--user", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="result string")
    def test_returns_run_cmd_output(self, _, __):
        """_run_cmd の戻り値が result.output に入る。"""
        result = call(**self._kw())
        self.assertTrue(result.success)
        self.assertEqual(result.output, "result string")

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_empty_output_uses_fallback_message(self, _, __):
        """出力が空文字のときはフォールバックメッセージを返す（空でない）。"""
        result = call(**self._kw())
        self.assertTrue(result.success)
        self.assertGreater(len(result.output), 0)

    # ---- arg_types 指定ありケース ---- #

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_typed_args_signature_in_cmd(self, mock_run, _):
        """arg_types を指定すると、結合シグネチャがコマンドに含まれる。"""
        call(**self._kw(args=["firefox"], arg_types=["s"]))
        cmd = mock_run.call_args[0][0]
        self.assertIn("s", cmd)
        self.assertIn("firefox", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_multiple_arg_types_joined_as_signature(self, mock_run, _):
        """複数の arg_types は結合されて 1 つのシグネチャ文字列になる。"""
        call(**self._kw(args=["hello", "42"], arg_types=["s", "i"]))
        cmd = mock_run.call_args[0][0]
        self.assertIn("si", cmd)

    # ---- auto-introspect ケース ---- #

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._introspect_method_signature", return_value="s")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_auto_introspect_calls_sig_helper(self, _run, mock_sig, _be):
        """args あり・arg_types なし: _introspect_method_signature が呼ばれる。"""
        call(**self._kw(args=["firefox"]))
        mock_sig.assert_called_once()

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._introspect_method_signature", return_value="s")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_auto_introspect_applies_signature_to_cmd(self, mock_run, _sig, _be):
        """introspect で取得したシグネチャが実際のコマンドに使われる。"""
        call(**self._kw(args=["firefox"]))
        cmd = mock_run.call_args[0][0]
        self.assertIn("s", cmd)
        self.assertIn("firefox", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._introspect_method_signature", return_value="a{sv}")
    def test_auto_introspect_complex_type_returns_error(self, _sig, _be):
        """複合型シグネチャは MVP 外としてエラーを返す。"""
        result = call(**self._kw(args=["foo"]))
        self.assertFalse(result.success)
        self.assertIn("複合型", result.error)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._introspect_method_signature", return_value=None)
    @patch("tools.dbus._run_cmd", return_value="")
    def test_auto_introspect_failure_falls_back_to_string(self, mock_run, _sig, _be):
        """introspect が None を返したとき、全引数を文字列として扱う。"""
        result = call(**self._kw(args=["firefox"]))
        self.assertTrue(result.success)
        cmd = mock_run.call_args[0][0]
        self.assertIn("s", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._introspect_method_signature", return_value="si")
    def test_auto_introspect_arg_count_mismatch(self, _sig, _be):
        """引数の数がシグネチャと一致しない場合はエラーを返す。"""
        # sig="si" は 2 引数必要、1 つしか渡さない
        result = call(**self._kw(args=["only_one"]))
        self.assertFalse(result.success)
        self.assertIn("引数の数", result.error)
        self.assertIn("si", result.error)

    # ---- qdbus フォールバック ---- #

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    @patch("tools.dbus._run_cmd", return_value="done")
    def test_qdbus_backend_uses_qdbus_cmd(self, mock_run, _):
        """qdbus バックエンドのとき、qdbus コマンドが使われる。"""
        result = call(**self._kw())
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "qdbus")
        self.assertTrue(result.success)

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    @patch("tools.dbus._run_cmd", return_value="done")
    def test_qdbus_cmd_includes_interface_dot_method(self, mock_run, _):
        """qdbus は 'interface.method' 形式でコマンドを組む。"""
        call(**self._kw())
        cmd = mock_run.call_args[0][0]
        self.assertIn("org.kde.krunner.App.CleanHistory", cmd)

    # ---- エラーケース ---- #

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", side_effect=RuntimeError("exit 1: no such method"))
    def test_command_failure_returns_error(self, _, __):
        """コマンド失敗時は success=False、error に内容が入る。"""
        result = call(**self._kw())
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    @patch("tools.dbus._detect_backend", side_effect=RuntimeError("no backend"))
    def test_no_backend_returns_error(self, _):
        """バックエンドなし: success=False を返す（例外を伝播させない）。"""
        result = call(**self._kw())
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)


# ------------------------------------------------------------------ #
# [A-6] TestDbusGetProperty
# ------------------------------------------------------------------ #

class TestDbusGetProperty(unittest.TestCase):
    """get_property() 関数のテスト。"""

    def _kw(self, **overrides):
        base = dict(
            service="org.kde.KWin",
            object_path="/KWin",
            interface="org.kde.KWin",
            prop="ActiveWindow",
        )
        return {**base, **overrides}

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="s \"abc123\"")
    def test_busctl_get_property_cmd(self, mock_run, _):
        """busctl get-property コマンドが正しく組み立てられる。"""
        get_property(**self._kw())
        cmd = mock_run.call_args[0][0]
        self.assertIn("get-property", cmd)
        self.assertIn("org.kde.KWin", cmd)
        self.assertIn("ActiveWindow", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_system_bus_flag(self, mock_run, _):
        get_property(**self._kw(bus="system"))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--system", cmd)

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    @patch("tools.dbus._run_cmd", return_value="42")
    def test_qdbus_get_cmd(self, mock_run, _):
        """qdbus バックエンドは 'interface.property' 形式を使う。"""
        get_property(**self._kw())
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "qdbus")
        self.assertIn("org.kde.KWin.ActiveWindow", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="s \"hello\"")
    def test_returns_output(self, _, __):
        result = get_property(**self._kw())
        self.assertTrue(result.success)
        self.assertEqual(result.output, "s \"hello\"")

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", side_effect=RuntimeError("no such property"))
    def test_command_failure_returns_error(self, _, __):
        result = get_property(**self._kw())
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)


# ------------------------------------------------------------------ #
# [A-7] TestDbusSetProperty
# ------------------------------------------------------------------ #

class TestDbusSetProperty(unittest.TestCase):
    """set_property() 関数のテスト。busctl 必須制約を含む。"""

    def _kw(self, **overrides):
        base = dict(
            service="org.kde.ScreenBrightness",
            object_path="/org/kde/ScreenBrightness",
            interface="org.kde.ScreenBrightness",
            prop="Brightness",
            args=[80],
        )
        return {**base, **overrides}

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_busctl_set_property_cmd(self, mock_run, _):
        """busctl set-property コマンドが正しく組み立てられる。"""
        set_property(**self._kw(arg_types=["i"]))
        cmd = mock_run.call_args[0][0]
        self.assertIn("set-property", cmd)
        self.assertIn("Brightness", cmd)
        self.assertIn("i", cmd)
        self.assertIn("80", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_default_type_is_string(self, mock_run, _):
        """arg_types 省略時はデフォルト型 "s" が使われる。"""
        set_property(**self._kw())  # arg_types なし
        cmd = mock_run.call_args[0][0]
        self.assertIn("s", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_system_bus_flag(self, mock_run, _):
        set_property(**self._kw(bus="system"))
        cmd = mock_run.call_args[0][0]
        self.assertIn("--system", cmd)

    def test_empty_args_returns_error(self):
        """args が空のときはエラーを返す（バックエンド呼び出し不要）。"""
        result = set_property(
            service="svc", object_path="/obj", interface="iface", prop="Prop",
            args=[],
        )
        self.assertFalse(result.success)
        self.assertIn("args", result.error)

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    def test_qdbus_backend_returns_error(self, _):
        """qdbus バックエンドでは set をサポートしないためエラーを返す。"""
        result = set_property(**self._kw())
        self.assertFalse(result.success)
        self.assertIn("busctl", result.error)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_empty_output_uses_fallback_message(self, _, __):
        """出力が空のときはフォールバックメッセージ（値を含む）を返す。"""
        result = set_property(**self._kw())
        self.assertTrue(result.success)
        self.assertIn("80", result.output)


# ------------------------------------------------------------------ #
# [A-8] TestDbusListServices
# ------------------------------------------------------------------ #

class TestDbusListServices(unittest.TestCase):
    """list_services() 関数のテスト。"""

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="org.freedesktop.DBus\norg.kde.KWin")
    def test_busctl_list_cmd(self, mock_run, _):
        """busctl list --no-pager コマンドが使われる。"""
        list_services()
        cmd = mock_run.call_args[0][0]
        self.assertIn("list", cmd)
        self.assertIn("--no-pager", cmd)
        self.assertIn("--user", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_system_bus_flag(self, mock_run, _):
        list_services(bus="system")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--system", cmd)

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    @patch("tools.dbus._run_cmd", return_value="org.kde.KWin")
    def test_qdbus_list_cmd(self, mock_run, _):
        """qdbus バックエンドは qdbus コマンド単体を使う。"""
        list_services()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["qdbus"])

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="org.freedesktop.DBus\norg.kde.KWin")
    def test_returns_output(self, _, __):
        result = list_services()
        self.assertTrue(result.success)
        self.assertIn("org.freedesktop.DBus", result.output)


# ------------------------------------------------------------------ #
# [A-9] TestDbusIntrospect
# ------------------------------------------------------------------ #

class TestDbusIntrospect(unittest.TestCase):
    """公開 introspect() アクションのテスト。"""

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value=".CleanHistory method - - -")
    def test_busctl_introspect_cmd_with_interface(self, mock_run, _):
        """interface を指定したとき、busctl introspect コマンドに含まれる。"""
        introspect("org.kde.krunner", "/App", interface="org.kde.krunner.App")
        cmd = mock_run.call_args[0][0]
        self.assertIn("introspect", cmd)
        self.assertIn("org.kde.krunner", cmd)
        self.assertIn("org.kde.krunner.App", cmd)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value=".CleanHistory method - - -")
    def test_busctl_introspect_cmd_without_interface(self, mock_run, _):
        """interface を省略したとき、busctl introspect コマンドに含まれない。"""
        introspect("org.kde.krunner", "/App")
        cmd = mock_run.call_args[0][0]
        self.assertIn("introspect", cmd)
        # interface が cmd の末尾に追加されないことを確認
        self.assertNotIn("org.kde.krunner.App", cmd)

    @patch("tools.dbus._detect_backend", return_value="qdbus")
    @patch("tools.dbus._run_cmd", return_value="org.kde.krunner.App.CleanHistory")
    def test_qdbus_introspect_cmd(self, mock_run, _):
        """qdbus バックエンドは qdbus <service> <object> を使う。"""
        introspect("org.kde.krunner", "/App")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "qdbus")

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", side_effect=RuntimeError("no such service"))
    def test_failure_returns_error(self, _, __):
        result = introspect("org.nonexistent", "/obj")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)

    @patch("tools.dbus._detect_backend", return_value="busctl")
    @patch("tools.dbus._run_cmd", return_value="")
    def test_system_bus_flag(self, mock_run, _):
        introspect("org.kde.krunner", "/App", bus="system")
        cmd = mock_run.call_args[0][0]
        self.assertIn("--system", cmd)


# ------------------------------------------------------------------ #
# [B] TestSafetyCheckerDbus
# ------------------------------------------------------------------ #

class TestSafetyCheckerDbus(unittest.TestCase):
    """
    safety.py の _check_dbus() 全分岐を検証する。
    test_safety.py の make_checker パターンを踏襲する。
    """

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _dbus_step(self, action: str, service: str = "org.kde.krunner", **kwargs) -> dict:
        return {"tool": "dbus", "action": action, "service": service, **kwargs}

    # ---- ブロックサービス ---- #

    def test_systemd_service_is_blocked(self):
        """org.freedesktop.systemd1 は blocked=True, Level 3。"""
        r = self.checker.check(self._dbus_step("call", "org.freedesktop.systemd1"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_login1_service_is_blocked(self):
        """org.freedesktop.login1 は blocked=True, Level 3。"""
        r = self.checker.check(self._dbus_step("call", "org.freedesktop.login1"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_unknown_service_is_not_blocked(self):
        """blocklist にないサービスはブロックされない。"""
        r = self.checker.check(self._dbus_step("call", "org.kde.krunner"))
        self.assertFalse(r.blocked)

    # ---- action ごとの danger_level ---- #

    def test_set_action_is_level2(self):
        """action=set はプロパティ変更 → Level 2。"""
        r = self.checker.check(self._dbus_step("set"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    def test_call_action_is_level1(self):
        """action=call はメソッド呼び出し → Level 1。"""
        r = self.checker.check(self._dbus_step("call"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_get_action_is_level0(self):
        """action=get は読み取り専用 → Level 0。"""
        r = self.checker.check(self._dbus_step("get"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_list_action_is_level0(self):
        """action=list は読み取り専用 → Level 0。"""
        r = self.checker.check(self._dbus_step("list"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_introspect_action_is_level0(self):
        """action=introspect は読み取り専用 → Level 0。"""
        r = self.checker.check(self._dbus_step("introspect"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    # ---- 優先順位 ---- #

    def test_blocked_service_beats_action_level(self):
        """
        ブロックサービスへの set（Level 2）は、
        サービスブロックが優先されて Level 3 になる。
        """
        r = self.checker.check(
            self._dbus_step("set", "org.freedesktop.systemd1")
        )
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    # ---- 設定バリエーション ---- #

    def test_empty_blocked_list_allows_all_services(self):
        """dbus_blocked_services が空のとき、全サービスが通る。"""
        cfg = dict(MINIMAL_DBUS_CONFIG)
        cfg["dbus_blocked_services"] = []
        checker, tmp = make_checker(cfg)
        try:
            r = checker.check(self._dbus_step("call", "org.freedesktop.systemd1"))
            self.assertFalse(r.blocked)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def test_dbus_step_skips_path_restriction(self):
        """
        dbus ステップは src/dst を持たないため、
        allowed_paths チェックに引っかからない。
        """
        r = self.checker.check(
            {"tool": "dbus", "action": "call", "service": "org.kde.KWin"}
            # src / dst なし
        )
        self.assertFalse(r.blocked)


# ------------------------------------------------------------------ #
# [C] 統合テスト
# ------------------------------------------------------------------ #

def _dbus_session_available() -> bool:
    """busctl が存在し、session bus に接続できるか確認する。"""
    if not shutil.which("busctl"):
        return False
    try:
        r = subprocess.run(
            ["busctl", "--user", "list", "--no-pager"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


@unittest.skipUnless(_dbus_session_available(), "D-Bus session not available")
class TestDbusIntegration(unittest.TestCase):
    """
    実 busctl を使ったスモークテスト。
    副作用ゼロの get / list のみ使用する。
    org.freedesktop.DBus は常駐確定サービス（busctl --user list で確認済み）。
    """

    def test_list_session_services_succeeds(self):
        """list_services() が成功し、org.freedesktop.DBus が含まれる。"""
        result = list_services(bus="session", timeout=10)
        self.assertTrue(result.success, msg=result.error)
        self.assertIn("org.freedesktop.DBus", result.output)

    def test_get_dbus_features_property(self):
        """
        org.freedesktop.DBus の Features プロパティを取得できる。
        Features は as（文字列配列）で返るが、出力は busctl テキスト形式。
        """
        result = get_property(
            service="org.freedesktop.DBus",
            object_path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            prop="Features",
            bus="session",
            timeout=10,
        )
        self.assertTrue(result.success, msg=result.error)
        self.assertGreater(len(result.output), 0)

    def test_introspect_dbus_interface(self):
        """
        org.freedesktop.DBus を introspect できる。
        出力に既知のメソッド名 'Hello' が含まれる。
        """
        result = introspect(
            service="org.freedesktop.DBus",
            object_path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus",
            bus="session",
            timeout=10,
        )
        self.assertTrue(result.success, msg=result.error)
        self.assertIn("Hello", result.output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
