"""
planner.py - OpenPaw Task Planner

qwen2.5:7b（Ollama経由）にタスクを渡し、JSONプランを生成する。
設計書 Section 4・5 準拠。

HTTP呼び出し部分は OllamaClient に分離しており、
テスト時はモックに差し替え可能。
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Protocol
from pathlib import Path

import os
from dotenv import load_dotenv
load_dotenv()


# ------------------------------------------------------------------ #
# デフォルト設定
# ------------------------------------------------------------------ #

DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL      = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

_HOME = Path.home()
_USER = _HOME.name

# LLMに渡すシステムプロンプト
SYSTEM_PROMPT = """あなたはLinuxデスクトップを操作するAIエージェントです。
ユーザーのタスクを分析し、実行ステップをJSONで返してください。
他のテキストは一切含めないでください。

使用できるツール: shell / filesystem / gui / dbus

gui ツールの action 一覧:
  screenshot（スクリーンショット撮影）, key（キー送信）, type（文字入力）,
  click（クリック）, move（マウス移動）, scroll（スクロール）,
  focus（ウィンドウフォーカス）, getwindows（ウィンドウ一覧）

重要: tool が "gui" のステップは "action" フィールドを使う。"command" フィールドは使わない。

正しい例（gui/screenshot）:
{"task_summary":"スクリーンショットを撮る","steps":[{"step_id":1,"tool":"gui","action":"screenshot","description":"スクリーンショットを撮影する","danger_level":0,"on_error":"abort"}]}

正しい例（gui/key）:
{"task_summary":"コピーする","steps":[{"step_id":1,"tool":"gui","action":"key","keys":"ctrl+c","description":"コピー","danger_level":1,"on_error":"abort"}]}

正しい例（shell）:
{"task_summary":"ファイル確認","steps":[{"step_id":1,"tool":"shell","command":"ls ~/Downloads","description":"一覧確認","danger_level":0,"on_error":"abort"}]}


正しい例（filesystem）:
{"task_summary":"PDFをDocumentsへ移動","steps":[{"step_id":1,"tool":"shell","command":"ls ~/Downloads/*.pdf","description":"PDFを確認","danger_level":0,"on_error":"abort"},{"step_id":2,"tool":"filesystem","action":"move","src":"~/Downloads/*.pdf","dst":"~/Documents/","description":"PDFを移動","danger_level":1,"on_error":"abort"}]}
誤った例（gui なのに command を使っている）:

正しい例（dbus）:
{"task_summary":"KRunnerの検索履歴をクリア","steps":[{"step_id":1,"tool":"dbus","action":"call","service":"org.kde.krunner","object":"/App","interface":"org.kde.krunner.App","method":"toggleDisplay","args":[],"bus":"session","description":"KRunnerの検索履歴をクリア","danger_level":1,"on_error":"abort"}]}
{"step_id":1,"tool":"gui","command":"gui:action=screenshot"} ← これは間違い

正しい例（gui なのに action を使っている）:
{"step_id":1,"tool":"gui","action":"screenshot"} ← これが正しい

ルール:
- スクリーンショットは必ず tool: gui, action: screenshot を使う
- gui ステップには action フィールドが必須（command は不要）
- shell ステップには command フィールドが必須
- danger_level は 0〜3 の整数
- パスは ~/ 形式を使う
""" + f"- 現在のユーザーは {_USER}、ホームディレクトリは {_HOME} です\n"


# ------------------------------------------------------------------ #
# OllamaClient Protocol（テスト時にモック差し替え可能）
# ------------------------------------------------------------------ #

class OllamaClientProtocol(Protocol):
    def generate(self, prompt: str) -> str:
        """プロンプトを送り、LLMの応答テキストを返す。"""
        ...


class OllamaClient:
    """
    Ollama の /api/generate エンドポイントを呼び出す実装。
    format: json で構造化出力を強制する。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.model    = model
        self.timeout  = timeout

    def generate(self, prompt: str) -> str:
        url = f"{self.base_url}/api/generate"
        payload = json.dumps({
            "model":  self.model,
            "prompt": prompt,
            "system": SYSTEM_PROMPT,
            "format": "json",
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["response"]


# ------------------------------------------------------------------ #
# Plan / PlannerError
# ------------------------------------------------------------------ #

@dataclass
class Plan:
    """LLMが生成したJSONプラン。"""
    task_summary: str
    steps: list[dict]
    raw: str  # デバッグ用の生JSON文字列


class PlannerError(Exception):
    """プラン生成・パース失敗時の例外。"""
    pass


# ------------------------------------------------------------------ #
# TaskPlanner
# ------------------------------------------------------------------ #

class TaskPlanner:
    """
    ユーザーのタスク文字列を受け取り、Ollama経由でJSONプランを生成する。

    Usage:
        planner = TaskPlanner()
        plan = planner.plan("Downloadsの古いPDFをDocumentsに移動して")
    """

    def __init__(self, client: Optional[OllamaClientProtocol] = None):
        self._client = client or OllamaClient()

    def plan(self, task: str) -> Plan:
        """
        タスク文字列からJSONプランを生成して返す。

        Args:
            task: ユーザーの自然言語タスク

        Returns:
            Plan

        Raises:
            PlannerError: Ollama接続失敗またはJSONパース失敗
        """
        prompt = f"タスク: {task}"

        try:
            raw = self._client.generate(prompt)
        except urllib.error.URLError as e:
            raise PlannerError(
                f"Ollama に接続できませんでした。起動しているか確認してください。\n詳細: {e}"
            ) from e
        except Exception as e:
            raise PlannerError(f"LLM呼び出しエラー: {e}") from e

        return self._parse(raw)

    def _parse(self, raw: str) -> Plan:
        """
        LLMの応答JSONをパースして Plan を返す。
        バリデーションも行い、不正な場合は PlannerError を送出する。
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlannerError(f"JSONパース失敗: {e}\n応答: {raw!r}") from e

        # 必須フィールドチェック
        if "task_summary" not in data:
            raise PlannerError(f"task_summary がありません: {raw!r}")
        if "steps" not in data or not isinstance(data["steps"], list):
            raise PlannerError(f"steps がありません: {raw!r}")
        if len(data["steps"]) == 0:
            raise PlannerError("steps が空です")

        # gui ステップの正規化（LLM が command に詰め込んだ場合の救済）
        for step in data["steps"]:
            self._normalize_gui_step(step)

        # 各ステップのバリデーション
        for i, step in enumerate(data["steps"], 1):
            self._validate_step(step, i)

        return Plan(
            task_summary=data["task_summary"],
            steps=data["steps"],
            raw=raw,
        )

    def _normalize_gui_step(self, step: dict) -> None:
        """
        LLM が gui ツールで action を command に詰め込んだ場合に救済する。
        例: {"tool":"gui","command":"gui:screenshot"} → {"tool":"gui","action":"screenshot"}
        """
        if step.get("tool") != "gui":
            return
        if "action" in step:
            return  # 既に正しい形式

        # command フィールドから action を推定する
        command = step.pop("command", "") or ""
        command_lower = command.lower()

        # "gui:screenshot" / "screenshot" / "gui:action=screenshot" などに対応
        action_map = {
            "screenshot": "screenshot",
            "getwindows": "getwindows",
            "key":        "key",
            "type":       "type",
            "click":      "click",
            "move":       "move",
            "scroll":     "scroll",
            "focus":      "focus",
        }
        for keyword, action in action_map.items():
            if keyword in command_lower:
                step["action"] = action
                return

        # 推定できなければ screenshot をデフォルトとして補完
        step["action"] = "screenshot"

    def _validate_step(self, step: dict, index: int) -> None:
        """1ステップのフィールドを検証する。"""
        required = ("step_id", "tool", "description", "danger_level", "on_error")
        for field in required:
            if field not in step:
                raise PlannerError(f"ステップ {index}: '{field}' フィールドがありません")

        tool = step["tool"]
        if tool not in ("shell", "filesystem", "dbus", "gui"):
            raise PlannerError(f"ステップ {index}: 不正な tool 値: {tool!r}")

        if tool == "shell" and "command" not in step:
            raise PlannerError(f"ステップ {index}: shell ツールに 'command' がありません")

        if tool == "filesystem":
            if "action" not in step:
                raise PlannerError(f"ステップ {index}: filesystem ツールに 'action' がありません")
            if "src" not in step:
                raise PlannerError(f"ステップ {index}: filesystem ツールに 'src' がありません")
            if step["action"] in ("move", "copy") and "dst" not in step:
                raise PlannerError(
                    f"ステップ {index}: filesystem {step['action']} に 'dst' がありません"
                )

        dl = step.get("danger_level")
        if not isinstance(dl, int) or dl < 0 or dl > 3:
            raise PlannerError(f"ステップ {index}: danger_level が不正: {dl!r}")
