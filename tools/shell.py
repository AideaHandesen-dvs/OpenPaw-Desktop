"""
tools/shell.py - OpenPaw Shell Tool

bashコマンドを subprocess で実行し、結果を返す。
タイムアウト・作業ディレクトリの制御を担う。
Safety Checker による事前検証は executor.py 側の責務とし、
このモジュールは「渡されたコマンドをそのまま実行する」に徹する。
"""

import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class ShellResult:
    """shell ツールの実行結果。"""
    success: bool
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    @property
    def output(self) -> str:
        """ログ・表示用の出力文字列（stdout を優先、なければ stderr）。"""
        if self.stdout.strip():
            return self.stdout.strip()
        if self.stderr.strip():
            return self.stderr.strip()
        return ""


def run(command: str, timeout: int = 30, cwd: Optional[str] = None) -> ShellResult:
    """
    bash コマンドを実行して ShellResult を返す。

    Args:
        command:  実行する bash コマンド文字列
        timeout:  タイムアウト秒数（デフォルト 30）
        cwd:      作業ディレクトリ（None でカレントディレクトリ）

    Returns:
        ShellResult
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return ShellResult(
            success=(proc.returncode == 0),
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )

    except subprocess.TimeoutExpired:
        return ShellResult(
            success=False,
            stdout="",
            stderr=f"Command timed out after {timeout} seconds.",
            returncode=-1,
            timed_out=True,
        )

    except Exception as e:  # noqa: BLE001
        return ShellResult(
            success=False,
            stdout="",
            stderr=str(e),
            returncode=-1,
        )
