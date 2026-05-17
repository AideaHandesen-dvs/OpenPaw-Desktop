"""
logger.py - OpenPaw Audit Logger

監査ログを ~/.openpaw/logs/YYYY-MM-DD.jsonl に1行1エントリで保存する。
設計書 Section 10 準拠。
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# JST タイムゾーン（設計書のサンプルが +09:00）
JST = timezone(timedelta(hours=9))

# デフォルトのログディレクトリ（テスト時に差し替え可能）
DEFAULT_LOG_DIR = Path.home() / ".openpaw" / "logs"


class AuditLogger:
    """
    1インスタンス = 1タスクのログセッション。
    各ステップの実行結果を append_step() で追記する。
    """

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)

        today = datetime.now(JST).strftime("%Y-%m-%d")
        self.log_file = self.log_dir / f"{today}.jsonl"

    def append_step(
        self,
        *,
        task_summary: str,
        step_id: int,
        tool: str,
        danger_level: int,
        status: str,
        user_confirmed: Optional[bool] = None,
        action: Optional[str] = None,
        command: Optional[str] = None,
        src: Optional[str] = None,
        dst: Optional[str] = None,
        output: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        entry: dict = {
            "timestamp": datetime.now(JST).isoformat(),
            "task_summary": task_summary,
            "step_id": step_id,
            "tool": tool,
            "danger_level": danger_level,
            "status": status,
        }
        if user_confirmed is not None:
            entry["user_confirmed"] = user_confirmed
        if action is not None:
            entry["action"] = action
        if command is not None:
            entry["command"] = command
        if src is not None:
            entry["src"] = src
        if dst is not None:
            entry["dst"] = dst
        if output is not None:
            entry["output"] = output
        if error is not None:
            entry["error"] = error

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def append_task_start(self, task_summary: str) -> None:
        entry = {
            "timestamp": datetime.now(JST).isoformat(),
            "event": "task_start",
            "task_summary": task_summary,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def append_task_end(self, task_summary: str, status: str) -> None:
        entry = {
            "timestamp": datetime.now(JST).isoformat(),
            "event": "task_end",
            "task_summary": task_summary,
            "status": status,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_log_path(self) -> Path:
        return self.log_file
