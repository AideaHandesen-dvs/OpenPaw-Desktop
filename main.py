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

        print(f"  危険度: {LEVEL_LABEL.get(level, str(level))}")
        print()


def ask_confirmation(task_summary: str) -> bool:
    """HITLの確認プロンプトを表示し、y なら True を返す。"""
    try:
        answer = input("実行しますか？ [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == "y"


# ------------------------------------------------------------------ #
# Tool Executor
# ------------------------------------------------------------------ #

def execute_step(step: dict) -> tuple[bool, str, str | None]:
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

    # ---- Step 6: HITL確認（Level >= 1 のステップがあれば） --------- #
    needs_confirmation = any(s["danger_level"] >= 1 for s in plan.steps)

    if needs_confirmation and not yes:
        confirmed = ask_confirmation(plan.task_summary)
        if not confirmed:
            print("[中断] ユーザーがキャンセルしました。")
            return 0
    else:
        confirmed = True  # --yes フラグまたは全ステップ Level 0

    # ---- Step 7: 実行 --------------------------------------------- #
    logger.append_task_start(plan.task_summary)

    for step in plan.steps:
        sid   = step["step_id"]
        tool  = step["tool"]
        level = step["danger_level"]

        print(f"[実行中] ステップ {sid}: {step['description']}")

        success, output, error = execute_step(step)

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
