"""
tools/filesystem.py - OpenPaw Filesystem Tool

copy / move / delete / mkdir の4操作を実装する。
グロブ展開・チルダ展開（~/...）・パス検証を担う。
Safety Checker によるパス制限チェックは executor.py 側の責務とし、
このモジュールは「渡されたパスに対して操作を実行する」に徹する。
"""

import glob
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class FilesystemResult:
    """filesystem ツールの実行結果。"""
    success: bool
    output: str              # ログ・表示用のメッセージ
    error: Optional[str] = None
    affected_paths: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ #
# 内部ユーティリティ
# ------------------------------------------------------------------ #

def _resolve(path: str) -> list[Path]:
    """
    チルダ展開 + グロブ展開を行い、マッチしたパスのリストを返す。
    グロブ文字がない場合でも必ずリストで返す（長さ0もあり得る）。
    """
    expanded = str(Path(path).expanduser())
    matched = glob.glob(expanded)
    if matched:
        return [Path(p) for p in matched]
    # グロブがマッチしなかった場合でも Path オブジェクトとして返す
    # （mkdir などグロブ不要な操作のため）
    return [Path(expanded)]


# ------------------------------------------------------------------ #
# 公開 API
# ------------------------------------------------------------------ #

def copy(src: str, dst: str) -> FilesystemResult:
    """
    src → dst へコピーする。
    src はグロブ可。dst がディレクトリなら中に展開する。

    Args:
        src: コピー元パス（グロブ可）
        dst: コピー先パスまたはディレクトリ
    """
    src_paths = _resolve(src)
    if not src_paths or not src_paths[0].exists():
        return FilesystemResult(
            success=False,
            output="",
            error=f"Source not found: {src}",
        )

    dst_path = Path(dst).expanduser()
    affected: list[str] = []

    try:
        for s in src_paths:
            if not s.exists():
                continue
            if dst_path.is_dir():
                target = dst_path / s.name
            else:
                target = dst_path

            if s.is_dir():
                shutil.copytree(s, target)
            else:
                shutil.copy2(s, target)
            affected.append(str(target))

        count = len(affected)
        return FilesystemResult(
            success=True,
            output=f"{count} file(s) copied",
            affected_paths=affected,
        )

    except Exception as e:  # noqa: BLE001
        return FilesystemResult(
            success=False,
            output="",
            error=str(e),
            affected_paths=affected,
        )


def move(src: str, dst: str) -> FilesystemResult:
    """
    src → dst へ移動する。
    src はグロブ可。dst がディレクトリなら中に展開する。

    Args:
        src: 移動元パス（グロブ可）
        dst: 移動先パスまたはディレクトリ
    """
    src_paths = _resolve(src)
    if not src_paths or not src_paths[0].exists():
        return FilesystemResult(
            success=False,
            output="",
            error=f"Source not found: {src}",
        )

    dst_path = Path(dst).expanduser()
    affected: list[str] = []

    try:
        for s in src_paths:
            if not s.exists():
                continue
            if dst_path.is_dir():
                target = dst_path / s.name
            else:
                target = dst_path

            shutil.move(str(s), str(target))
            affected.append(str(target))

        count = len(affected)
        return FilesystemResult(
            success=True,
            output=f"{count} file(s) moved",
            affected_paths=affected,
        )

    except Exception as e:  # noqa: BLE001
        return FilesystemResult(
            success=False,
            output="",
            error=str(e),
            affected_paths=affected,
        )


def delete(src: str) -> FilesystemResult:
    """
    src を削除する（ファイルまたはディレクトリ）。
    src はグロブ可。ディレクトリは再帰削除。

    Args:
        src: 削除対象パス（グロブ可）
    """
    src_paths = _resolve(src)
    if not src_paths or not src_paths[0].exists():
        return FilesystemResult(
            success=False,
            output="",
            error=f"Source not found: {src}",
        )

    affected: list[str] = []

    try:
        for s in src_paths:
            if not s.exists():
                continue
            if s.is_dir():
                shutil.rmtree(s)
            else:
                s.unlink()
            affected.append(str(s))

        count = len(affected)
        return FilesystemResult(
            success=True,
            output=f"{count} item(s) deleted",
            affected_paths=affected,
        )

    except Exception as e:  # noqa: BLE001
        return FilesystemResult(
            success=False,
            output="",
            error=str(e),
            affected_paths=affected,
        )


def mkdir(path: str) -> FilesystemResult:
    """
    ディレクトリを作成する（parents=True、exist_ok=True）。

    Args:
        path: 作成するディレクトリパス
    """
    target = Path(path).expanduser()

    try:
        target.mkdir(parents=True, exist_ok=True)
        return FilesystemResult(
            success=True,
            output=f"Directory created: {target}",
            affected_paths=[str(target)],
        )

    except Exception as e:  # noqa: BLE001
        return FilesystemResult(
            success=False,
            output="",
            error=str(e),
        )
