"""
safety.py - OpenPaw Safety Checker

設計書 Section 6・7 に従い、各ステップの danger_level を判定し、
実行可否を決定する。

チェック順序（設計書 Section 7 準拠）：
  1. blocklist に一致 → Level 3、即中断
  2. パス制限チェック → allowed_paths 外なら中断
  3. dangerous パターンに一致 → Level 2
  4. filesystem の delete 操作 → Level 2
  5. filesystem の move / copy 操作 → Level 1
  6. それ以外 → Level 0
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


# ------------------------------------------------------------------ #
# デフォルト設定ファイルパス
# ------------------------------------------------------------------ #

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "safety.yaml"


# ------------------------------------------------------------------ #
# データクラス
# ------------------------------------------------------------------ #

@dataclass
class CheckResult:
    """Safety Checker の判定結果。"""
    danger_level: int          # 0〜3
    blocked: bool              # True なら実行不可
    reason: Optional[str]      # blocked / 警告時のメッセージ


# ------------------------------------------------------------------ #
# SafetyChecker
# ------------------------------------------------------------------ #

class SafetyChecker:
    """
    safety.yaml を読み込み、ステップの安全性を検証する。

    Usage:
        checker = SafetyChecker()
        result = checker.check(step)
        if result.blocked:
            print(result.reason)
    """

    def __init__(self, config_path: Optional[Path] = None):
        import os
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        cfg_text = path.read_text(encoding="utf-8")
        # ${HOME} / ${USER} を実行ユーザーの値に展開する
        cfg_text = cfg_text.replace("${HOME}", str(Path.home()))
        cfg_text = cfg_text.replace("${USER}", os.getenv("USER", Path.home().name))
        cfg = yaml.safe_load(cfg_text)

        self._blocklist: list[str]           = cfg.get("blocklist", [])
        self._dangerous: list[str]           = cfg.get("dangerous", [])
        self._allowed_paths: list[str]       = cfg.get("allowed_paths", [])
        self._dbus_blocked_services: list[str] = cfg.get("dbus_blocked_services", [])
        self._gui_blocked_keys: list[str]    = cfg.get("gui_blocked_keys", [])
        self._gui_dangerous_keys: list[str]  = cfg.get("gui_dangerous_keys", [])
        self.timeout: int                    = int(cfg.get("timeout", 30))

    # ------------------------------------------------------------------ #
    # 公開 API
    # ------------------------------------------------------------------ #

    def check(self, step: dict) -> CheckResult:
        """
        ステップ dict を受け取り、CheckResult を返す。

        Args:
            step: JSON プランの1ステップ（設計書 Section 5 のスキーマ）
        """
        tool    = step.get("tool", "")
        command = step.get("command", "")
        action  = step.get("action", "")
        src     = step.get("src", "")
        dst     = step.get("dst", "")

        # ---- 1. blocklist ----------------------------------------- #
        if self._is_blocked(command):
            return CheckResult(
                danger_level=3,
                blocked=True,
                reason=f"Blocked by blocklist: {command!r}",
            )

        # ---- 1.5. dbus サービスブロック --------------------------- #
        if tool == "dbus":
            return self._check_dbus(step)

        # ---- 1.6. gui アクションチェック -------------------------- #
        if tool == "gui":
            return self._check_gui(step)

        # ---- 2. パス制限チェック ----------------------------------- #
        paths_to_check = [p for p in (src, dst) if p and p != "$prev"]
        for p in paths_to_check:
            if not self._is_allowed_path(p):
                return CheckResult(
                    danger_level=3,
                    blocked=True,
                    reason=f"Path not in allowed_paths: {p!r}",
                )

        # ---- 3. dangerous パターン --------------------------------- #
        if self._is_dangerous(command):
            return CheckResult(
                danger_level=2,
                blocked=False,
                reason=f"Matches dangerous pattern: {command!r}",
            )

        # ---- 4. filesystem delete ---------------------------------- #
        if tool == "filesystem" and action == "delete":
            return CheckResult(
                danger_level=2,
                blocked=False,
                reason="Filesystem delete operation",
            )

        # ---- 5. filesystem move / copy ----------------------------- #
        if tool == "filesystem" and action in ("move", "copy"):
            return CheckResult(
                danger_level=1,
                blocked=False,
                reason=None,
            )

        # ---- 6. それ以外 ------------------------------------------ #
        return CheckResult(
            danger_level=0,
            blocked=False,
            reason=None,
        )

    # ------------------------------------------------------------------ #
    # 内部メソッド
    # ------------------------------------------------------------------ #

    def _is_blocked(self, command: str) -> bool:
        """blocklist のいずれかのパターンがコマンドに含まれるか判定する。

        単一単語パターン（"sudo", "mkfs" 等）はトークン単位で一致させる。
        ただし "mkfs.ext4" のような派生コマンドも捕捉するため、
        トークンがパターンで始まり次の文字が英数字でない場合もブロック対象とする。
        複数単語パターン（"rm -rf /" 等）はホワイトスペース正規化後に部分一致。
        これにより "sudoku" のような誤検知を防ぎつつ、スペース重複・派生コマンドの回避も防ぐ。
        """
        if not command:
            return False
        # ホワイトスペース正規化（スペース重複・タブによる回避を防ぐ）
        normalized = " ".join(command.split())
        tokens = normalized.split()

        for pattern in self._blocklist:
            pattern_normalized = " ".join(pattern.split())
            if " " in pattern_normalized:
                # 複数単語パターン → 正規化済み文字列の部分一致
                if pattern_normalized in normalized:
                    return True
            else:
                # 単一単語パターン → トークンの完全一致 or 派生コマンド前方一致
                # 例: "mkfs" → "mkfs" も "mkfs.ext4" もブロック
                # 例: "sudo" → "sudo" はブロック、"sudoku" はスキップ
                for token in tokens:
                    if token == pattern_normalized or token.startswith(pattern_normalized + "."):
                        return True
        return False

    def _is_dangerous(self, command: str) -> bool:
        """
        dangerous リストのいずれかのパターンがコマンドに一致するか判定する。
        先頭一致（prefix）または部分一致で判定する。
        """
        if not command:
            return False
        cmd_stripped = command.strip()
        for pattern in self._dangerous:
            # コマンドが pattern で始まるか、または空白区切りの最初のトークンが一致
            if cmd_stripped == pattern or cmd_stripped.startswith(pattern + " "):
                return True
            # 複数単語パターン（"systemctl stop" など）は部分一致
            if " " in pattern and pattern in cmd_stripped:
                return True
        return False

    def _is_allowed_path(self, path: str) -> bool:
        """
        パスが allowed_paths のいずれかのプレフィックスに収まるか判定する。
        Path.resolve() で ../../../ 等のパストラバーサルを無力化する。
        存在しないパスも strict=False（デフォルト）で展開される。
        """
        if not self._allowed_paths:
            return True  # allowed_paths が空なら全パスを許可

        # グロブ文字を含む場合は親ディレクトリで判定
        # 例: ~/Downloads/*.pdf → ~/Downloads/ を対象にする
        check_path = path
        if "*" in path or "?" in path:
            check_path = path.split("*")[0].split("?")[0]

        try:
            resolved = Path(check_path).expanduser().resolve()
        except Exception:
            return False

        for allowed in self._allowed_paths:
            try:
                allowed_resolved = Path(allowed).expanduser().resolve()
            except Exception:
                continue
            if resolved == allowed_resolved or str(resolved).startswith(
                str(allowed_resolved) + "/"
            ):
                return True

        return False

    def _check_dbus(self, step: dict) -> CheckResult:
        """
        dbus ツール固有の安全性チェック。

        チェック順:
          1. ブロック対象サービス → Level 3、blocked
          2. action == set        → Level 2（プロパティ変更）
          3. action == call       → Level 1（メソッド呼び出し）
          4. action == get / list → Level 0（読み取り専用）
        """
        service = step.get("service", "")
        action  = step.get("action", "")

        # ブロック対象サービス
        if service and service in self._dbus_blocked_services:
            return CheckResult(
                danger_level=3,
                blocked=True,
                reason=f"D-Bus service blocked: {service!r}",
            )

        # set はプロパティ変更 → Level 2
        if action == "set":
            return CheckResult(
                danger_level=2,
                blocked=False,
                reason="D-Bus set-property operation",
            )

        # call はメソッド呼び出し → Level 1
        if action == "call":
            return CheckResult(
                danger_level=1,
                blocked=False,
                reason=None,
            )

        # get / list は読み取り → Level 0
        return CheckResult(
            danger_level=0,
            blocked=False,
            reason=None,
        )

    def _check_gui(self, step: dict) -> CheckResult:
        """
        gui ツール固有の安全性チェック。

        チェック順:
          1. gui_blocked_keys に一致するキー → Level 3、blocked
          2. gui_dangerous_keys に一致するキー → Level 2
          3. 読み取り専用アクション (getwindows / screenshot / move) → Level 0
          4. 入力系アクション (key / type / click / scroll / focus) → Level 1
        """
        action = step.get("action", "")
        keys   = step.get("keys", "")

        # ---- 1. ブロック対象キーコンビネーション ------------------- #
        for blocked in self._gui_blocked_keys:
            if blocked.lower() in keys.lower():
                return CheckResult(
                    danger_level=3,
                    blocked=True,
                    reason=f"GUI key blocked by config: {keys!r}",
                )

        # ---- 2. 危険キーコンビネーション → Level 2 ----------------- #
        for dangerous in self._gui_dangerous_keys:
            if dangerous.lower() in keys.lower():
                return CheckResult(
                    danger_level=2,
                    blocked=False,
                    reason=f"GUI dangerous key combination: {keys!r}",
                )

        # ---- 3. 読み取り専用アクション → Level 0 ------------------- #
        read_only_actions = {"getwindows", "screenshot", "move"}
        if action in read_only_actions:
            return CheckResult(
                danger_level=0,
                blocked=False,
                reason=None,
            )

        # ---- 4. 入力系アクション → Level 1 ------------------------- #
        # key / type / click / scroll / focus はユーザー確認が必要
        return CheckResult(
            danger_level=1,
            blocked=False,
            reason=None,
        )
