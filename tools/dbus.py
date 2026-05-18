"""
tools/dbus.py - OpenPaw D-Bus Tool

busctl（優先）または qdbus（フォールバック）を使って D-Bus を操作する。
Safety Checker によるサービスブロックは safety.py 側の責務とし、
このモジュールは「渡されたパラメータに対して操作を実行する」に徹する。

サポートするアクション:
  call       - メソッド呼び出し    (busctl / qdbus)
  get        - プロパティ取得      (busctl / qdbus)
  set        - プロパティ設定      (busctl のみ)
  list       - サービス一覧        (busctl / qdbus)
  introspect - メソッド/プロパティ一覧（デバッグ・補助用）

### auto-introspect（call アクションの特殊挙動）
args あり・arg_types なし の場合、busctl introspect でシグネチャを自動取得する。

  単純型のみ (y b n q i u x t d s h) → 自動適用
  複合型含む (a ( { v など)          → エラー返却（MVP スコープ外）
  introspect 失敗                     → 全引数を文字列 "s" にフォールバック

arg_types が明示されていれば introspect はスキップする。
"""

from __future__ import annotations

import shutil
import subprocess
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 定数
# ------------------------------------------------------------------ #

# D-Bus 単純型コード（1文字 = 1引数）
# y=byte  b=bool  n=int16  q=uint16  i=int32  u=uint32
# x=int64 t=uint64 d=double s=string  h=unix_fd
_SIMPLE_DBUS_TYPES = frozenset("ybnqiuxtdsh")


# ------------------------------------------------------------------ #
# 結果データクラス
# ------------------------------------------------------------------ #

@dataclass
class DbusResult:
    """dbus ツールの実行結果。"""
    success: bool
    output: str              # ログ・表示用メッセージ
    error: Optional[str] = None


# ------------------------------------------------------------------ #
# バックエンド検出
# ------------------------------------------------------------------ #

def _detect_backend() -> str:
    """
    利用可能な D-Bus バックエンドを返す。busctl > qdbus の優先順。
    どちらも無ければ RuntimeError。
    """
    if shutil.which("busctl"):
        return "busctl"
    if shutil.which("qdbus"):
        return "qdbus"
    raise RuntimeError(
        "D-Busバックエンドが見つかりません。"
        "'busctl'（systemd）または 'qdbus'（kde-cli-tools）を"
        "インストールしてください。"
    )


# ------------------------------------------------------------------ #
# 型シグネチャヘルパー
# ------------------------------------------------------------------ #

def _is_simple_signature(sig: str) -> bool:
    """
    シグネチャが単純型のみで構成されているか確認する。
    True の場合、len(sig) == 引数の個数。

    単純型: y b n q i u x t d s h（1文字=1引数）
    複合型: a ( { v など（MVP スコープ外）
    """
    return bool(sig) and all(c in _SIMPLE_DBUS_TYPES for c in sig)


def _introspect_method_signature(
    service: str,
    object_path: str,
    interface: str,
    method: str,
    bus: str,
    timeout: int,
) -> Optional[str]:
    """
    busctl introspect でメソッドの入力型シグネチャを取得する。
    取得できない場合・qdbus バックエンドの場合は None を返す。

    busctl introspect の出力フォーマット:
      NAME              TYPE    SIGNATURE  RESULT/VALUE  FLAGS
      .CleanHistory     method  -          -             -
      .Query            method  s          as            -

    Returns:
        シグネチャ文字列（例: "si"）。引数なし("-")・取得失敗の場合は None。
    """
    try:
        backend = _detect_backend()
        if backend != "busctl":
            return None  # qdbus のパース形式は異なるため非対応

        output = _run_cmd(
            ["busctl", _busctl_flag(bus),
             "introspect", service, object_path, interface],
            timeout,
        )
    except RuntimeError:
        return None

    for line in output.splitlines():
        parts = line.split()
        if (len(parts) >= 3
                and parts[0] == f".{method}"
                and parts[1] == "method"):
            sig = parts[2]
            return None if sig == "-" else sig

    return None  # メソッドが出力に見つからなかった


# ------------------------------------------------------------------ #
# 内部: subprocess 実行
# ------------------------------------------------------------------ #

def _run_cmd(cmd: list[str], timeout: int) -> str:
    """コマンドを実行して stdout を返す。失敗時は RuntimeError。"""
    logger.debug("[dbus] cmd: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"タイムアウト（{timeout}秒）: {' '.join(cmd)}")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"コマンド失敗 (exit {result.returncode}): "
            f"{stderr or '(no stderr)'}"
        )

    return result.stdout.strip()


# ------------------------------------------------------------------ #
# 内部: busctl バックエンド
# ------------------------------------------------------------------ #

def _busctl_flag(bus: str) -> str:
    return "--user" if bus == "session" else "--system"


def _busctl_call(
    service: str, object_path: str, interface: str, method: str,
    args: list, arg_types: list[str], bus: str, timeout: int,
) -> str:
    cmd = [
        "busctl", _busctl_flag(bus),
        "call", service, object_path, interface, method,
    ]
    if arg_types and args:
        cmd += ["".join(arg_types)] + [str(a) for a in args]
    elif args:
        cmd += ["s" * len(args)] + [str(a) for a in args]
    return _run_cmd(cmd, timeout)


def _busctl_get(
    service: str, object_path: str, interface: str, prop: str,
    bus: str, timeout: int,
) -> str:
    cmd = [
        "busctl", _busctl_flag(bus),
        "get-property", service, object_path, interface, prop,
    ]
    return _run_cmd(cmd, timeout)


def _busctl_set(
    service: str, object_path: str, interface: str, prop: str,
    args: list, arg_types: list[str], bus: str, timeout: int,
) -> str:
    signature = arg_types[0] if arg_types else "s"
    cmd = [
        "busctl", _busctl_flag(bus),
        "set-property", service, object_path, interface, prop,
        signature, str(args[0]),
    ]
    return _run_cmd(cmd, timeout)


def _busctl_list(bus: str, timeout: int) -> str:
    cmd = ["busctl", _busctl_flag(bus), "list", "--no-pager"]
    return _run_cmd(cmd, timeout)


def _busctl_introspect(
    service: str, object_path: str, interface: str, bus: str, timeout: int,
) -> str:
    cmd = ["busctl", _busctl_flag(bus), "introspect", service, object_path]
    if interface:
        cmd.append(interface)
    return _run_cmd(cmd, timeout)


# ------------------------------------------------------------------ #
# 内部: qdbus フォールバック
# ------------------------------------------------------------------ #

def _qdbus_call(
    service: str, object_path: str, interface: str, method: str,
    args: list, timeout: int,
) -> str:
    cmd = ["qdbus", service, object_path, f"{interface}.{method}"] + [str(a) for a in args]
    return _run_cmd(cmd, timeout)


def _qdbus_get(
    service: str, object_path: str, interface: str, prop: str, timeout: int,
) -> str:
    cmd = ["qdbus", service, object_path, f"{interface}.{prop}"]
    return _run_cmd(cmd, timeout)


def _qdbus_list(timeout: int) -> str:
    return _run_cmd(["qdbus"], timeout)


# ------------------------------------------------------------------ #
# 公開 API - filesystem.py と対称なアクション別関数
# ------------------------------------------------------------------ #

def call(
    service: str,
    object_path: str,
    interface: str,
    method: str,
    args: Optional[list] = None,
    arg_types: Optional[list[str]] = None,
    bus: str = "session",
    timeout: int = 30,
) -> DbusResult:
    """
    D-Bus メソッドを呼び出す。

    Args:
        service:     バス名          e.g. "org.kde.krunner"
        object_path: オブジェクトパス e.g. "/App"
        interface:   インターフェース e.g. "org.kde.krunner.App"
        method:      メソッド名       e.g. "CleanHistory"
        args:        引数リスト
        arg_types:   busctl 型シグネチャ e.g. ["s", "i"]
                     省略時は introspect で自動取得を試みる
        bus:         "session" | "system"
        timeout:     タイムアウト秒
    """
    args      = args or []
    arg_types = arg_types or []

    # ---- auto-introspect ----------------------------------------- #
    if args and not arg_types:
        sig = _introspect_method_signature(
            service, object_path, interface, method, bus, timeout
        )

        if sig is None:
            # introspect 失敗 → 全引数を文字列として扱うフォールバック
            logger.warning(
                "[dbus] '%s' の introspect 失敗 → 全引数を文字列として扱います",
                method,
            )
            arg_types = ["s"] * len(args)

        elif _is_simple_signature(sig):
            # 単純型: 引数の個数チェック後に適用
            if len(sig) != len(args):
                return DbusResult(
                    success=False, output="",
                    error=(
                        f"[dbus] 引数の数が一致しません: "
                        f"'{method}' は {len(sig)} 個の引数を期待しますが "
                        f"{len(args)} 個が渡されました（シグネチャ: '{sig}'）"
                    ),
                )
            arg_types = list(sig)

        else:
            # 複合型 (a, (, {, v など) は MVP スコープ外
            return DbusResult(
                success=False, output="",
                error=(
                    f"[dbus] メソッド '{method}' のシグネチャ '{sig}' に"
                    f"複合型が含まれています。"
                    f"'arg_types' を手動で指定するか、"
                    f"busctl コマンドを直接参照してください。"
                ),
            )

    # ---- 実行 ---------------------------------------------------- #
    try:
        backend = _detect_backend()
        if backend == "busctl":
            output = _busctl_call(
                service, object_path, interface, method,
                args, arg_types, bus, timeout,
            )
        else:
            output = _qdbus_call(
                service, object_path, interface, method, args, timeout,
            )
        logger.info("[dbus] call %s.%s → OK", interface, method)
        return DbusResult(
            success=True,
            output=output or f"called: {interface}.{method}",
        )

    except Exception as e:  # noqa: BLE001
        return DbusResult(success=False, output="", error=str(e))


def get_property(
    service: str,
    object_path: str,
    interface: str,
    prop: str,
    bus: str = "session",
    timeout: int = 30,
) -> DbusResult:
    """D-Bus プロパティを取得する。"""
    try:
        backend = _detect_backend()
        if backend == "busctl":
            output = _busctl_get(service, object_path, interface, prop, bus, timeout)
        else:
            output = _qdbus_get(service, object_path, interface, prop, timeout)
        logger.info("[dbus] get %s.%s → %s", interface, prop, output[:60])
        return DbusResult(success=True, output=output)

    except Exception as e:  # noqa: BLE001
        return DbusResult(success=False, output="", error=str(e))


def set_property(
    service: str,
    object_path: str,
    interface: str,
    prop: str,
    args: list,
    arg_types: Optional[list[str]] = None,
    bus: str = "session",
    timeout: int = 30,
) -> DbusResult:
    """D-Bus プロパティを設定する（busctl のみ対応）。"""
    arg_types = arg_types or []

    if not args:
        return DbusResult(
            success=False, output="",
            error="[dbus] set_property には 'args' が必要です。",
        )

    try:
        backend = _detect_backend()
        if backend != "busctl":
            raise RuntimeError(
                "'set' アクションは busctl が必要です（qdbus は非対応）。"
                "systemd パッケージをインストールしてください。"
            )
        output = _busctl_set(
            service, object_path, interface, prop,
            args, arg_types, bus, timeout,
        )
        logger.info("[dbus] set %s.%s → OK", interface, prop)
        return DbusResult(
            success=True,
            output=output or f"set: {interface}.{prop} = {args[0]}",
        )

    except Exception as e:  # noqa: BLE001
        return DbusResult(success=False, output="", error=str(e))


def list_services(
    bus: str = "session",
    timeout: int = 30,
) -> DbusResult:
    """バス上のサービス一覧を返す。"""
    try:
        backend = _detect_backend()
        if backend == "busctl":
            output = _busctl_list(bus, timeout)
        else:
            output = _qdbus_list(timeout)
        logger.info("[dbus] list (%s bus) → %d lines", bus, len(output.splitlines()))
        return DbusResult(success=True, output=output)

    except Exception as e:  # noqa: BLE001
        return DbusResult(success=False, output="", error=str(e))


def introspect(
    service: str,
    object_path: str,
    interface: str = "",
    bus: str = "session",
    timeout: int = 30,
) -> DbusResult:
    """
    D-Bus インターフェースのメソッド・プロパティ一覧を返す。
    デバッグ・プランナー補助用。interface を省略すると全インターフェースを表示。
    """
    try:
        backend = _detect_backend()
        if backend == "busctl":
            output = _busctl_introspect(service, object_path, interface, bus, timeout)
        else:
            output = _run_cmd(["qdbus", service, object_path], timeout)
        logger.info("[dbus] introspect %s %s", service, object_path)
        return DbusResult(success=True, output=output)

    except Exception as e:  # noqa: BLE001
        return DbusResult(success=False, output="", error=str(e))
