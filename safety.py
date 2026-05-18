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
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self._blocklist: list[str]           = cfg.get("blocklist", [])
        self._dangerous: list[str]           = cfg.get("dangerous", [])
        self._allowed_paths: list[str]       = cfg.get("allowed_paths", [])
        self._dbus_blocked_services: list[str] = cfg.get("dbus_blocked_services", [])
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

        # ---- 2. パス制限チェック ----------------------------------- #
        paths_to_check = [p for p in (src, dst) if p]
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
        """blocklist のいずれかのパターンがコマンドに含まれるか判定する。"""
        if not command:
            return False
        for pattern in self._blocklist:
            if pattern in command:
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
        チルダ展開は行わず、文字列プレフィックスで比較する。
        （チルダ展開は executor.py 側の責務）
        """
        if not self._allowed_paths:
            return True  # allowed_paths が空なら全パスを許可

        # チルダを /home/<username> 相当に展開して比較
        expanded = str(Path(path).expanduser())

        for allowed in self._allowed_paths:
            allowed_expanded = str(Path(allowed).expanduser())
            if expanded == allowed_expanded or expanded.startswith(allowed_expanded + "/"):
                return True

        # グロブ文字を含む場合は親ディレクトリで判定
        # 例: ~/Downloads/*.pdf → ~/Downloads/ が allowed かチェック
        if "*" in path or "?" in path:
            parent = str(Path(path.split("*")[0].split("?")[0]).expanduser())
            for allowed in self._allowed_paths:
                allowed_expanded = str(Path(allowed).expanduser())
                if parent.startswith(allowed_expanded):
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
