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


正しい例（filesystem - ファイル移動）:
{"task_summary":"test.txtをDocumentsへ移動","steps":[{"step_id":1,"tool":"filesystem","action":"move","src":"~/test.txt","dst":"~/Documents/","description":"test.txtを移動","danger_level":1,"on_error":"abort"}]}

注意: ファイルのコピー・移動・削除は shell の mv/cp/rm ではなく必ず filesystem ツールを使う。

正しい例（filesystem - PDFをDocumentsへ移動）:
{"task_summary":"PDFをDocumentsへ移動","steps":[{"step_id":1,"tool":"shell","command":"ls ~/Downloads/*.pdf","description":"PDFを確認","danger_level":0,"on_error":"abort"},{"step_id":2,"tool":"filesystem","action":"move","src":"~/Downloads/*.pdf","dst":"~/Documents/","description":"PDFを移動","danger_level":1,"on_error":"abort"}]}

正しい例（条件付き移動 - 古いPDFだけDocumentsへ移動）:
{"task_summary":"古いPDFをDocumentsへ移動","steps":[{"step_id":1,"tool":"shell","command":"find ~/Downloads -name '*.pdf' -mtime +30","description":"30日以上前のPDFを検索","danger_level":0,"capture_output":true,"on_error":"abort"},{"step_id":2,"tool":"filesystem","action":"move","src":"$prev","dst":"~/Documents/","description":"検索結果のPDFを移動","danger_level":1,"on_error":"abort"}]}

重要: 「古いファイルだけ」「サイズが大きいものだけ」など絞り込みが必要な場合:
- Step1: shell で find コマンドを使い、\"capture_output\": true を必ず付ける
- Step2: filesystem の src を \"$prev\" にする（前ステップの出力がそのまま渡される）
- $prev は必ず src フィールドにのみ使う。dst には使わない
誤った例（gui なのに command を使っている）:

正しい例（dbus）:
{"task_summary":"KRunnerを開閉する","steps":[{"step_id":1,"tool":"dbus","action":"call","service":"org.kde.krunner","object":"/App","interface":"org.kde.krunner.App","method":"toggleDisplay","args":[],"arg_types":[],"bus":"session","description":"KRunnerを開閉する","danger_level":1,"on_error":"abort"}]}
{"step_id":1,"tool":"gui","command":"gui:action=screenshot"} ← これは間違い

正しい例（gui なのに action を使っている）:
{"step_id":1,"tool":"gui","action":"screenshot"} ← これが正しい

ルール:
- スクリーンショットは必ず tool: gui, action: screenshot を使う
- gui ステップには action フィールドが必須（command は不要）
- shell ステップには command フィールドが必須
- danger_level は 0〜3 の整数
- パスは ~/ 形式を使う
- dbus を使う場合、提示されたメソッド一覧に存在するメソッドのみを使うこと
- 提示されたメソッドでタスクを達成できない場合は dbus を使わず shell または filesystem を使うこと
""" + f"- 現在のユーザーは {_USER}、ホームディレクトリは {_HOME} です\n"


# D-Bus ターゲット検出専用プロンプト（第1パスで使用）
DBUS_DETECT_PROMPT = """ユーザーのタスクにD-Busが必要か判断し、JSONのみを返してください。

D-Busが不要（ファイル操作・シェル・GUI）な場合:
{"use_dbus": false}

D-Busが必要（KDEサービス操作・システムサービス制御など）な場合:
{"use_dbus": true, "service": "org.kde.krunner", "object": "/App", "bus": "session"}

serviceとobjectは推測で構いません。後でintrospectして確認します。
他のテキストは一切含めないでください。"""


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
        """通常プランニング用（SYSTEM_PROMPTを使用）。"""
        return self.generate_with_system(prompt, SYSTEM_PROMPT)

    def generate_with_system(self, prompt: str, system: str) -> str:
        """任意のシステムプロンプトでLLMを呼び出す。"""
        url = f"{self.base_url}/api/generate"
        payload = json.dumps({
            "model":  self.model,
            "prompt": prompt,
            "system": system,
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

        D-Bus タスクの場合は二段階プランニングを行う:
          1. D-Bus ターゲット（service/object）を LLM に推定させる
          2. Python が introspect を実行して実在メソッド一覧を取得
          3. メソッド一覧を含めた上で LLM にプランを生成させる
          4. 生成されたプランの dbus ステップが実在メソッドを使っているか
             コードレベルで検証し、違反があれば shell で再プランニングする（BUG-006対策）

        generate_with_system() を持たないクライアント（モック）では
        ステップ1〜2をスキップして通常の単一パス動作にフォールバックする。

        Args:
            task: ユーザーの自然言語タスク

        Returns:
            Plan

        Raises:
            PlannerError: Ollama接続失敗またはJSONパース失敗
        """
        # ---- D-Bus 二段階プランニング ----------------------------- #
        dbus_context = ""
        valid_dbus_methods: set[str] = set()
        if hasattr(self._client, "generate_with_system"):
            target = self._detect_dbus_target(task)
            if target.get("use_dbus"):
                methods = self._introspect_for_planner(target)
                if methods:
                    valid_dbus_methods = self._parse_method_names(methods)
                    svc = target.get("service", "")
                    obj = target.get("object", "")
                    dbus_context = (
                        f"\n\nD-Busメソッド一覧 ({svc} {obj}):\n{methods}\n"
                        f"重要: 上記のメソッドのみ使用可能です。"
                        f"タスクを達成できるメソッドが一覧にない場合は"
                        f"dbus を使わず shell または filesystem でタスクを実行してください。"
                        f"存在しないメソッドを作ってはいけません。"
                    )

        # ---- 通常プランニング（第2パスまたは単一パス） ------------ #
        prompt = f"タスク: {task}{dbus_context}"

        try:
            raw = self._client.generate(prompt)
        except urllib.error.URLError as e:
            raise PlannerError(
                f"Ollama に接続できませんでした。起動しているか確認してください。\n詳細: {e}"
            ) from e
        except Exception as e:
            raise PlannerError(f"LLM呼び出しエラー: {e}") from e

        plan = self._parse(raw)

        # ---- D-Bus メソッド検証（コードレベルで強制、BUG-006対策）-- #
        # valid_dbus_methods が空でない = introspect 成功済み
        # その場合のみ、存在しないメソッドを使うステップを検出して
        # shell で再プランニングする。
        if valid_dbus_methods:
            plan = self._enforce_dbus_methods(task, plan, valid_dbus_methods)

        return plan

    def _parse_method_names(self, methods_str: str) -> set[str]:
        """
        _introspect_for_planner が返す文字列からメソッド名のセットを抽出する。

        入力例:
          .toggleDisplay    method    -    -    -
          .query            method    s    as   -

        戻り値: {"toggleDisplay", "query"}
        """
        names: set[str] = set()
        for line in methods_str.splitlines():
            parts = line.split()
            if (len(parts) >= 2
                    and parts[0].startswith(".")
                    and parts[1] == "method"):
                names.add(parts[0][1:])  # 先頭の "." を除去
        return names

    def _enforce_dbus_methods(
        self,
        task: str,
        plan: Plan,
        valid_methods: set[str],
    ) -> Plan:
        """
        生成されたプランの dbus/call ステップを検証し、
        メソッドが実在しない場合は shell で再プランニングする（BUG-006対策）。

        Args:
            task:          元のユーザータスク文字列
            plan:          LLM が生成したプラン
            valid_methods: introspect で確認済みのメソッド名セット

        Returns:
            検証済み（または再プランニング済み）の Plan

        Raises:
            PlannerError: 再プランニングも失敗した場合
        """
        invalid_steps = [
            step for step in plan.steps
            if step.get("tool") == "dbus"
            and step.get("action") == "call"
            and step.get("method", "") not in valid_methods
        ]
        if not invalid_steps:
            return plan  # 全ステップが有効

        # 存在しないメソッド名を列挙してフォールバック再プランニング
        bad_methods = [s.get("method", "(不明)") for s in invalid_steps]
        fallback_prompt = (
            f"タスク: {task}\n\n"
            f"D-Busを調査しましたが、タスクに必要なメソッドが存在しません"
            f"（存在しないメソッド: {', '.join(bad_methods)}）。\n"
            f"dbus を使わず、shell または filesystem ツールのみでタスクを実行してください。"
        )
        try:
            raw = self._client.generate(fallback_prompt)
            return self._parse(raw)
        except Exception as e:
            raise PlannerError(
                f"shellフォールバック再プランニング失敗: {e}\n"
                f"（元の無効メソッド: {', '.join(bad_methods)}）"
            ) from e

    def _detect_dbus_target(self, task: str) -> dict:
        """
        D-Bus が必要かどうかと、使用するサービス/オブジェクトを LLM に推定させる。
        失敗時は {"use_dbus": False} を返す（例外を外に出さない）。
        """
        try:
            raw = self._client.generate_with_system(  # type: ignore[attr-defined]
                f"タスク: {task}",
                DBUS_DETECT_PROMPT,
            )
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"use_dbus": False}
            return data
        except Exception:
            return {"use_dbus": False}

    def _introspect_for_planner(self, target: dict) -> str:
        """
        D-Bus サービスを introspect してメソッド一覧を文字列で返す。
        失敗時は空文字列を返す（例外を外に出さない）。

        busctl introspect の出力からメソッド行のみを抽出する:
          .toggleDisplay    method    -    -    -
          .query            method    s    as   -
        """
        from tools import dbus as dbus_tool

        service = target.get("service", "")
        obj     = target.get("object", "")
        bus     = target.get("bus", "session")

        if not service or not obj:
            return ""

        try:
            result = dbus_tool.introspect(
                service=service,
                object_path=obj,
                interface="",
                bus=bus,
                timeout=10,
            )
        except Exception:
            return ""

        if not result.success:
            return ""

        # メソッド行を抽出。標準 DBus インターフェース（org.freedesktop.*）は除外する。
        # busctl の出力はインターフェース行 → メソッド行の順なので、
        # 現在のインターフェースを追跡してフィルタリングする。
        SKIP_PREFIXES = (
            "org.freedesktop.DBus.Properties",
            "org.freedesktop.DBus.Introspectable",
            "org.freedesktop.DBus.Peer",
        )
        skip_section = False
        lines = []
        for line in result.output.splitlines():
            parts = line.split()
            if not parts:
                continue
            # インターフェース行（先頭がドットなし、2列目が "interface"）
            if len(parts) >= 2 and parts[1] == "interface":
                skip_section = any(parts[0].startswith(p) for p in SKIP_PREFIXES)
                continue
            # メソッド行
            if (not skip_section
                    and len(parts) >= 2
                    and parts[0].startswith(".")
                    and parts[1] == "method"):
                lines.append(line.strip())

        return "\n".join(lines)

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
