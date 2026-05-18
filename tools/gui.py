"""
tools/gui.py - OpenPaw GUI Tool

ydotool（Wayland優先）または xdotool（X11フォールバック）を使って
GUI を自動操作する。KDE Plasma + Debian 環境向け。

注意: このツールは Phase 3「保証なし」の位置付けである。
  - ydotool は ydotoold デーモンが起動している必要がある
  - xdotool は X11 セッションでのみ動作する（Wayland 非対応）
  - セッション種別・実行環境によって動作しない場合がある

サポートするアクション:
  key        - キーボード入力送信   (ydotool / xdotool)
  type       - テキスト入力         (ydotool / xdotool)
  click      - マウスクリック        (ydotool / xdotool)
  move       - マウス移動           (ydotool / xdotool)
  scroll     - スクロール           (ydotool / xdotool)
  focus      - ウィンドウフォーカス  (wmctrl / xdotool)
  screenshot - スクリーンショット    (scrot / spectacle / import)
  getwindows - ウィンドウ一覧        (wmctrl / xdotool)

JSONスキーマ（各アクションのフィールド）:
  key:        keys (str)  e.g. "ctrl+c", "Return", "super+d"
  type:       text (str), delay (int, ms/文字 デフォルト12)
  click:      x (int), y (int), button (int 1=左 2=中 3=右), count (int)
  move:       x (int), y (int)
  scroll:     direction (up/down/left/right), amount (int デフォルト3)
  focus:      target (str, ウィンドウタイトル)
  screenshot: path (str|null, 省略時は ~/.openpaw/screenshots/<timestamp>.png)
  getwindows: （引数なし）
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# 結果データクラス
# ------------------------------------------------------------------ #

@dataclass
class GuiResult:
    """gui ツールの実行結果。"""
    success: bool
    output: str              # ログ・表示用メッセージ
    error: Optional[str] = None


# ------------------------------------------------------------------ #
# バックエンド検出
# ------------------------------------------------------------------ #

def _detect_input_backend() -> Optional[str]:
    """
    キーボード・マウス入力バックエンドを返す。
    ydotool（Wayland対応） > xdotool（X11）の優先順。
    どちらも無ければ None。
    """
    if shutil.which("ydotool"):
        return "ydotool"
    if shutil.which("xdotool"):
        return "xdotool"
    return None


def _detect_window_backend() -> Optional[str]:
    """
    ウィンドウ管理バックエンドを返す。
    wmctrl > xdotool の優先順。
    どちらも無ければ None。
    """
    if shutil.which("wmctrl"):
        return "wmctrl"
    if shutil.which("xdotool"):
        return "xdotool"
    return None


def _detect_screenshot_backend() -> Optional[str]:
    """
    スクリーンショットバックエンドを返す。
    scrot > spectacle（KDE）> import（ImageMagick）の優先順。
    どれも無ければ None。
    """
    for cmd in ("scrot", "spectacle", "import"):
        if shutil.which(cmd):
            return cmd
    return None


# ------------------------------------------------------------------ #
# 内部ユーティリティ
# ------------------------------------------------------------------ #

def _run(cmd: str, timeout: int) -> GuiResult:
    """
    bash コマンドを実行して GuiResult を返す。
    全 GUI アクションの共通実行層。
    """
    logger.debug("gui cmd: %s", cmd)
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip() or "(OK)"
            return GuiResult(success=True, output=out)
        else:
            err = proc.stderr.strip() or f"exit code {proc.returncode}"
            return GuiResult(success=False, output="", error=err)

    except subprocess.TimeoutExpired:
        return GuiResult(
            success=False, output="",
            error=f"タイムアウト ({timeout}秒)",
        )
    except Exception as e:  # noqa: BLE001
        return GuiResult(success=False, output="", error=str(e))


def _no_input_backend() -> GuiResult:
    return GuiResult(
        success=False, output="",
        error=(
            "キー/マウス入力ツールが見つかりません。"
            " ydotool（Wayland）または xdotool（X11）をインストールしてください。"
        ),
    )


def _no_window_backend() -> GuiResult:
    return GuiResult(
        success=False, output="",
        error=(
            "ウィンドウ管理ツールが見つかりません。"
            " wmctrl または xdotool をインストールしてください。"
        ),
    )


# ------------------------------------------------------------------ #
# 公開 API
# ------------------------------------------------------------------ #

def key(keys: str, timeout: int = 30) -> GuiResult:
    """
    キーボードショートカット・特殊キーを送信する。

    Args:
        keys:    xdotool 形式のキー名 e.g. "ctrl+c", "Return", "super+d"
        timeout: タイムアウト秒数

    Examples:
        key("ctrl+c")       # コピー
        key("alt+F4")       # ウィンドウを閉じる
        key("super")        # アプリランチャー
        key("ctrl+alt+t")   # ターミナル起動（DE依存）
    """
    backend = _detect_input_backend()
    if backend is None:
        return _no_input_backend()

    # ydotool / xdotool どちらも同じキー名形式を受け付ける
    if backend == "ydotool":
        cmd = f"ydotool key {shlex.quote(keys)}"
    else:
        cmd = f"xdotool key {shlex.quote(keys)}"

    result = _run(cmd, timeout)
    if result.success:
        result.output = f"key sent: {keys}"
    return result


def type_text(text: str, delay: int = 12, timeout: int = 30) -> GuiResult:
    """
    テキストを1文字ずつキー入力として送信する。

    Args:
        text:    入力するテキスト
        delay:   文字間ディレイ（ミリ秒、デフォルト12）
        timeout: タイムアウト秒数
    """
    backend = _detect_input_backend()
    if backend is None:
        return _no_input_backend()

    if backend == "ydotool":
        # -- で引数終端を明示してテキストをそのまま渡す
        cmd = f"ydotool type --delay {delay} -- {shlex.quote(text)}"
    else:
        cmd = f"xdotool type --delay {delay} -- {shlex.quote(text)}"

    result = _run(cmd, timeout)
    if result.success:
        preview = text[:30] + ("..." if len(text) > 30 else "")
        result.output = f"typed: {preview!r}"
    return result


def click(
    x: int,
    y: int,
    button: int = 1,
    count: int = 1,
    timeout: int = 30,
) -> GuiResult:
    """
    指定座標でマウスクリックする。

    Args:
        x, y:    クリック座標（画面絶対座標）
        button:  1=左クリック, 2=中クリック, 3=右クリック（デフォルト1）
        count:   クリック回数（ダブルクリックは count=2）
        timeout: タイムアウト秒数
    """
    if button not in (1, 2, 3):
        return GuiResult(success=False, output="", error=f"不正な button 値: {button}（1/2/3のみ有効）")
    if count < 1:
        return GuiResult(success=False, output="", error=f"不正な count 値: {count}（1以上が必要）")

    backend = _detect_input_backend()
    if backend is None:
        return _no_input_backend()

    if backend == "ydotool":
        # ydotool のボタン番号: 1=left, 2=right, 3=middle
        # 本 API は 1=左, 2=中, 3=右 なので 2 と 3 を変換する
        ydotool_btn = {1: 1, 2: 3, 3: 2}[button]
        # mousemove は <x> <y> のみ（--absolute フラグなし）
        # click は <button> のみ（--count フラグなし）、count 回ループ
        click_cmds = " && ".join(f"ydotool click {ydotool_btn}" for _ in range(count))
        cmd = f"ydotool mousemove {x} {y} && {click_cmds}"
    else:
        # xdotool: 移動＋クリックを1コマンドで
        cmd = (
            f"xdotool mousemove {x} {y} "
            f"click --clearmodifiers --repeat {count} {button}"
        )

    result = _run(cmd, timeout)
    if result.success:
        btn_name = {1: "左", 2: "中", 3: "右"}[button]
        result.output = f"{btn_name}クリック x={x} y={y}" + (f" × {count}" if count > 1 else "")
    return result


def move(x: int, y: int, timeout: int = 30) -> GuiResult:
    """
    マウスカーソルを指定座標へ移動する（クリックなし）。

    Args:
        x, y:    移動先座標（画面絶対座標）
        timeout: タイムアウト秒数
    """
    backend = _detect_input_backend()
    if backend is None:
        return _no_input_backend()

    if backend == "ydotool":
        cmd = f"ydotool mousemove {x} {y}"
    else:
        cmd = f"xdotool mousemove {x} {y}"

    result = _run(cmd, timeout)
    if result.success:
        result.output = f"mouse moved to x={x} y={y}"
    return result


def scroll(direction: str, amount: int = 3, timeout: int = 30) -> GuiResult:
    """
    マウスホイールスクロールを送信する。

    Args:
        direction: "up" / "down" / "left" / "right"
        amount:    スクロール量（ステップ数、デフォルト3）
        timeout:   タイムアウト秒数
    """
    valid_directions = ("up", "down", "left", "right")
    if direction not in valid_directions:
        return GuiResult(
            success=False, output="",
            error=f"不正な direction: {direction!r}（{'/'.join(valid_directions)}のみ有効）",
        )
    if amount < 1:
        return GuiResult(success=False, output="", error=f"不正な amount: {amount}（1以上が必要）")

    backend = _detect_input_backend()
    if backend is None:
        return _no_input_backend()

    # ydotool に scroll サブコマンドは存在しない（0.1.x 系）
    # xdotool でスクロールボタン番号をエミュレートする
    # ydotool が検出されていても scroll は xdotool にフォールバック
    if not shutil.which("xdotool"):
        return GuiResult(
            success=False, output="",
            error="scroll には xdotool が必要です。sudo apt install xdotool でインストールしてください。",
        )
    # xdotool: ボタン 4=上, 5=下, 6=左, 7=右
    btn = {"up": 4, "down": 5, "left": 6, "right": 7}[direction]
    cmd = f"xdotool click --clearmodifiers --repeat {amount} {btn}"

    result = _run(cmd, timeout)
    if result.success:
        result.output = f"scroll {direction} × {amount}"
    return result


def focus_window(target: str, timeout: int = 30) -> GuiResult:
    """
    タイトルに target を含むウィンドウにフォーカスを当てる。
    大文字小文字を区別しない部分一致（wmctrl の場合）。

    Args:
        target:  ウィンドウタイトルの一部または全部
        timeout: タイムアウト秒数
    """
    backend = _detect_window_backend()
    if backend is None:
        return _no_window_backend()

    if backend == "wmctrl":
        # -a: アクティブにする（タイトル部分一致）
        cmd = f"wmctrl -a {shlex.quote(target)}"
    else:
        # xdotool: 名前検索 → フォーカス
        cmd = (
            f"xdotool search --onlyvisible --name {shlex.quote(target)} "
            f"windowfocus --sync"
        )

    result = _run(cmd, timeout)
    if result.success:
        result.output = f"focused: {target!r}"
    return result


def screenshot(path: Optional[str] = None, timeout: int = 30) -> GuiResult:
    """
    画面全体のスクリーンショットを保存する。

    Args:
        path:    保存先パス（省略時は ~/.openpaw/screenshots/<timestamp>.png）
        timeout: タイムアウト秒数

    Returns:
        GuiResult.output に実際の保存パスが入る。
    """
    backend = _detect_screenshot_backend()
    if backend is None:
        return GuiResult(
            success=False, output="",
            error=(
                "スクリーンショットツールが見つかりません。"
                " scrot / spectacle / import（ImageMagick）のいずれかを"
                "インストールしてください。"
            ),
        )

    # 保存先パスの決定
    if path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_dir = Path.home() / ".openpaw" / "screenshots"
        save_dir.mkdir(parents=True, exist_ok=True)
        path = str(save_dir / f"{ts}.png")

    if backend == "scrot":
        cmd = f"scrot {shlex.quote(path)}"
    elif backend == "spectacle":
        # -b: バックグラウンド実行、-o: 出力ファイル指定
        cmd = f"spectacle -b -o {shlex.quote(path)}"
    else:
        # import (ImageMagick): ルートウィンドウ全体をキャプチャ
        cmd = f"import -window root {shlex.quote(path)}"

    result = _run(cmd, timeout)
    if result.success:
        result.output = f"screenshot saved: {path}"
    return result


def get_windows(timeout: int = 30) -> GuiResult:
    """
    現在開いているウィンドウの一覧を取得する（読み取り専用）。

    Returns:
        GuiResult.output にウィンドウ一覧（テキスト）が入る。
    """
    backend = _detect_window_backend()
    if backend is None:
        return _no_window_backend()

    if backend == "wmctrl":
        # -l: ウィンドウ一覧（ID・デスクトップ番号・ホスト・タイトル）
        cmd = "wmctrl -l"
    else:
        # xdotool: 表示中ウィンドウのタイトルを一覧
        cmd = "xdotool search --onlyvisible --name '' getwindowname %@"

    return _run(cmd, timeout)
