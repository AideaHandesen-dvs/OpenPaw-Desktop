"""
main.py - OpenPaw エントリポイント

CLI引数を受け取り、Planner → Safety → HITL → Executor → Logger の
全体フローを制御する。
設計書 Section 4・8・9 準拠。

使い方:
    python main.py "Downloadsの古いPDFをDocumentsに移動して"
    python main.py --dry-run "Downloadsの古いPDFをDocumentsに移動して"
    python main.py --yes "tmpフォルダを空にして"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from logger import AuditLogger
from planner import TaskPlanner, PlannerError
from safety import SafetyChecker
from tools import shell as shell_tool
from tools import filesystem as fs_tool
from tools import dbus as dbus_tool
from tools import gui as gui_tool


# ------------------------------------------------------------------ #
# 定数
# ------------------------------------------------------------------ #

# danger_level の表示ラベル
LEVEL_LABEL = {
    0: "0（自動実行）",
    1: "1 ⚠  確認が必要です",
    2: "2 🔴 危険操作・要確認",
    3: "3 ⛔ ブロック",
}


# ------------------------------------------------------------------ #
# 表示ヘルパー
# ------------------------------------------------------------------ #

def print_plan(task_summary: str, steps: list[dict]) -> None:
    """プラン全体をユーザーに見やすく表示する。"""
    print(f"\n[OpenPaw] タスク: {task_summary}\n")
    total = len(steps)
    for step in steps:
        sid   = step["step_id"]
        tool  = step["tool"]
        desc  = step["description"]
        level = step.get("danger_level", 0)

        print(f"ステップ {sid}/{total}: {desc}")
        print(f"  ツール: {tool}")

        if tool == "shell":
            print(f"  コマンド: {step.get('command', '')}")
        elif tool == "filesystem":
            action = step.get("action", "")
            src    = step.get("src", "")
            dst    = step.get("dst", "")
            if dst:
                print(f"  操作: {action} {src} → {dst}")
            else:
                print(f"  操作: {action} {src}")
        elif tool == "dbus":
            action    = step.get("action", "")
            service   = step.get("service", "")
            interface = step.get("interface", "")
            method    = step.get("method", step.get("property", ""))
            bus       = step.get("bus", "session")
            if action == "introspect":
                print(f"  操作: introspect {service}")
                if interface:
                    print(f"  対象: {interface} ({bus} bus)")
            else:
                print(f"  操作: {action} {service}")
                if interface and method:
                    print(f"  対象: {interface}.{method} ({bus} bus)")

        elif tool == "gui":
            action = step.get("action", "")
            if action == "key":
                print(f"  操作: key {step.get('keys', '')}")
            elif action == "type":
                text = step.get("text", "")
                preview = text[:40] + ("..." if len(text) > 40 else "")
                print(f"  操作: type {preview!r}")
            elif action == "click":
                btn_name = {1: "左", 2: "中", 3: "右"}.get(step.get("button", 1), "?")
                count = step.get("count", 1)
                times = f" × {count}" if count > 1 else ""
                print(f"  操作: click({btn_name}) x={step.get('x', 0)} y={step.get('y', 0)}{times}")
            elif action == "move":
                print(f"  操作: move x={step.get('x', 0)} y={step.get('y', 0)}")
            elif action == "scroll":
                print(f"  操作: scroll {step.get('direction', '')} × {step.get('amount', 3)}")
            elif action == "focus":
                print(f"  操作: focus {step.get('target', '')!r}")
            elif action == "screenshot":
                path = step.get("path") or "~/.openpaw/screenshots/<timestamp>.png"
                print(f"  操作: screenshot → {path}")
            elif action == "getwindows":
                print(f"  操作: getwindows（ウィンドウ一覧取得）")
            else:
                print(f"  操作: {action}")

        print(f"  危険度: {LEVEL_LABEL.get(level, str(level))}")
        print()


def ask_confirmation(task_summary: str, level2_steps: list[dict] | None = None) -> bool:
    """HITLの確認プロンプトを表示し、y なら True を返す。

    level2_steps が指定されている場合は danger_level 2 のステップを明示する。
    --yes フラグでも level2_steps がある場合は必ず確認を求める（呼び出し側で制御）。
    """
    if level2_steps:
        print("⚠️  以下の危険操作が含まれています（--yes フラグでもスキップ不可）:")
        for step in level2_steps:
            sid  = step["step_id"]
            desc = step["description"]
            tool = step["tool"]
            if tool == "shell":
                detail = f"コマンド: {step.get('command', '')}"
            elif tool == "filesystem":
                detail = f"操作: {step.get('action', '')} {step.get('src', '')}"
            else:
                detail = f"ツール: {tool}"
            print(f"  ステップ {sid}: {desc}  [{detail}]")
        print()

    try:
        answer = input("実行しますか？ [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == "y"


# ------------------------------------------------------------------ #
# Tool Executor
# ------------------------------------------------------------------ #

def execute_step(step: dict, timeout: int = 30) -> tuple[bool, str, str | None]:
    """
    1ステップを実行し (success, output, error) を返す。

    Returns:
        (success, output, error)
    """
    tool = step["tool"]

    if tool == "shell":
        result = shell_tool.run(step["command"])
        return result.success, result.output, result.stderr if not result.success else None

    if tool == "filesystem":
        action = step["action"]
        src    = step.get("src", "")
        dst    = step.get("dst", "")

        if action == "copy":
            r = fs_tool.copy(src, dst)
        elif action == "move":
            r = fs_tool.move(src, dst)
        elif action == "delete":
            r = fs_tool.delete(src)
        elif action == "mkdir":
            r = fs_tool.mkdir(src)
        else:
            return False, "", f"未知の action: {action}"

        return r.success, r.output, r.error

    if tool == "dbus":
        action    = step.get("action", "")
        service   = step.get("service", "")
        obj       = step.get("object", "")
        interface = step.get("interface", "")
        args      = step.get("args", [])
        arg_types = step.get("arg_types", [])
        bus       = step.get("bus", "session")

        if action == "call":
            r = dbus_tool.call(service, obj, interface,
                               step.get("method", ""),
                               args, arg_types, bus, timeout)
        elif action == "get":
            r = dbus_tool.get_property(service, obj, interface,
                                       step.get("property", ""),
                                       bus, timeout)
        elif action == "set":
            r = dbus_tool.set_property(service, obj, interface,
                                       step.get("property", ""),
                                       args, arg_types, bus, timeout)
        elif action == "list":
            r = dbus_tool.list_services(bus, timeout)
        elif action == "introspect":
            r = dbus_tool.introspect(service, obj, interface, bus, timeout)
        else:
            return False, "", f"未知の dbus action: {action}"

        return r.success, r.output, r.error

    if tool == "gui":
        action = step.get("action", "")

        if action == "key":
            r = gui_tool.key(step.get("keys", ""), timeout)
        elif action == "type":
            r = gui_tool.type_text(step.get("text", ""), step.get("delay", 12), timeout)
        elif action == "click":
            r = gui_tool.click(
                step.get("x", 0), step.get("y", 0),
                step.get("button", 1), step.get("count", 1), timeout,
            )
        elif action == "move":
            r = gui_tool.move(step.get("x", 0), step.get("y", 0), timeout)
        elif action == "scroll":
            r = gui_tool.scroll(step.get("direction", "down"), step.get("amount", 3), timeout)
        elif action == "focus":
            r = gui_tool.focus_window(step.get("target", ""), timeout)
        elif action == "screenshot":
            r = gui_tool.screenshot(step.get("path"), timeout)
        elif action == "getwindows":
            r = gui_tool.get_windows(timeout)
        else:
            return False, "", f"未知の gui action: {action}"

        return r.success, r.output, r.error

    return False, "", f"未対応のツール: {tool}"


# ------------------------------------------------------------------ #
# メインフロー
# ------------------------------------------------------------------ #

def run(task: str, dry_run: bool = False, yes: bool = False) -> int:
    """
    OpenPaw のメインフロー。
    戻り値は終了コード（0=成功、1=失敗・中断）。
    """
    logger  = AuditLogger()
    checker = SafetyChecker()
    planner = TaskPlanner()

    # ---- Step 1: プラン生成 --------------------------------------- #
    print(f"[OpenPaw] タスクを解析中: {task}")
    try:
        plan = planner.plan(task)
    except PlannerError as e:
        print(f"\n[エラー] プラン生成失敗:\n{e}", file=sys.stderr)
        return 1

    # ---- Step 2: Safety Check ------------------------------------- #
    for step in plan.steps:
        result = checker.check(step)
        # LLMが設定した danger_level より Safety Checker の判定を優先
        step["danger_level"] = result.danger_level
        step["_blocked"]     = result.blocked
        step["_safe_reason"] = result.reason

    # ---- Step 3: プラン表示 --------------------------------------- #
    print_plan(plan.task_summary, plan.steps)

    # ---- Step 4: ブロックされたステップがあれば即中断 ------------- #
    for step in plan.steps:
        if step.get("_blocked"):
            print(
                f"[中断] ステップ {step['step_id']} がブロックされました: "
                f"{step['_safe_reason']}",
                file=sys.stderr,
            )
            logger.append_task_start(plan.task_summary)
            logger.append_step(
                task_summary=plan.task_summary,
                step_id=step["step_id"],
                tool=step["tool"],
                danger_level=step["danger_level"],
                status="aborted",
                error=step["_safe_reason"],
            )
            logger.append_task_end(plan.task_summary, "aborted")
            return 1

    # ---- Step 5: dry-run ------------------------------------------ #
    if dry_run:
        print("[dry-run] 実行はスキップされました。")
        return 0

    # ---- Step 6: HITL確認 ----------------------------------------- #
    # danger_level 2 のステップは --yes フラグでも確認を省略できない
    level2_steps   = [s for s in plan.steps if s["danger_level"] >= 2]
    level1_or_more = any(s["danger_level"] >= 1 for s in plan.steps)

    if level2_steps:
        # Level 2 が含まれる → --yes に関わらず確認必須
        confirmed = ask_confirmation(plan.task_summary, level2_steps=level2_steps)
        if not confirmed:
            print("[中断] ユーザーがキャンセルしました。")
            return 0
    elif level1_or_more and not yes:
        # Level 1 のみ、かつ --yes なし → 通常確認
        confirmed = ask_confirmation(plan.task_summary)
        if not confirmed:
            print("[中断] ユーザーがキャンセルしました。")
            return 0
    else:
        confirmed = True  # --yes フラグまたは全ステップ Level 0

    # ---- Step 7: 実行 --------------------------------------------- #
    logger.append_task_start(plan.task_summary)

    prev_output: str | None = None  # 直前ステップの出力（capture_output=true のとき保持）

    for step in plan.steps:
        sid   = step["step_id"]
        tool  = step["tool"]
        level = step["danger_level"]

        # $prev を前ステップの出力で置換
        if step.get("src") == "$prev":
            if prev_output is None:
                err_msg = (
                    f"ステップ {sid}: src=\"$prev\" が指定されていますが、"
                    "直前ステップに capture_output がないか出力が空です。"
                )
                print(f"[失敗] {err_msg}", file=sys.stderr)
                logger.append_task_end(plan.task_summary, "aborted")
                return 1

            lines = [l for l in prev_output.strip().splitlines() if l.strip()]

            if not lines:
                # 前ステップの出力が空 = 対象ファイルなし → スキップして正常終了
                print(f"[スキップ] ステップ {sid}: 対象ファイルなし（前ステップの出力が空）")
                logger.append_step(
                    task_summary=plan.task_summary,
                    step_id=sid,
                    tool=tool,
                    danger_level=level,
                    status="skipped",
                    user_confirmed=confirmed if level >= 1 else None,
                    action=step.get("action"),
                    src="$prev (empty)",
                )
                prev_output = None
                continue

            if len(lines) > 1:
                # 複数ファイル → 1件ずつ実行
                print(f"[実行中] ステップ {sid}: {step['description']} ({len(lines)} 件)")
                for path in lines:
                    sub_step = {**step, "src": path}
                    success, output, error = execute_step(sub_step, timeout=checker.timeout)
                    status = "success" if success else "failure"
                    logger.append_step(
                        task_summary=plan.task_summary,
                        step_id=sid,
                        tool=tool,
                        danger_level=level,
                        status=status,
                        user_confirmed=confirmed if level >= 1 else None,
                        action=step.get("action"),
                        command=step.get("command"),
                        src=path,
                        dst=step.get("dst"),
                        output=output or None,
                        error=error or None,
                    )
                    if success:
                        print(f"  → {path}")
                    else:
                        print(f"[失敗] ステップ {sid} ({path}): {error}", file=sys.stderr)
                        logger.append_task_end(plan.task_summary, "aborted")
                        return 1
                prev_output = None
                continue  # 通常の execute_step をスキップ
            else:
                step["src"] = lines[0] if lines else ""

        print(f"[実行中] ステップ {sid}: {step['description']}")

        success, output, error = execute_step(step, timeout=checker.timeout)

        status = "success" if success else "failure"

        logger.append_step(
            task_summary=plan.task_summary,
            step_id=sid,
            tool=tool,
            danger_level=level,
            status=status,
            user_confirmed=confirmed if level >= 1 else None,
            action=step.get("action"),
            command=step.get("command"),
            src=step.get("src"),
            dst=step.get("dst"),
            output=output or None,
            error=error or None,
        )

        if success:
            if output:
                print(f"  → {output}")
            # capture_output フラグがあれば出力を次ステップへ引き渡す
            if step.get("capture_output"):
                prev_output = output
            else:
                prev_output = None
        else:
            print(f"[失敗] ステップ {sid}: {error}", file=sys.stderr)
            logger.append_task_end(plan.task_summary, "aborted")
            return 1

    logger.append_task_end(plan.task_summary, "completed")
    print(f"\n[完了] {plan.task_summary}")
    print(f"ログ: {logger.get_log_path()}")
    return 0


# ------------------------------------------------------------------ #
# CLI エントリポイント
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openpaw",
        description="ローカルAIエージェントによるLinuxデスクトップ自動操作",
    )
    parser.add_argument("task", help="実行したいタスクを自然言語で指定")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="プランを表示するだけで実行しない",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="すべての確認をスキップして実行（上級者向け）",
    )

    args = parser.parse_args()
    sys.exit(run(args.task, dry_run=args.dry_run, yes=args.yes))


if __name__ == "__main__":
    main()
