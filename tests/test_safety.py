"""
tests/test_safety.py - SafetyChecker の単体テスト
設計書 Section 6・7 のチェック順序をすべて検証する。
"""

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from safety import SafetyChecker


# ------------------------------------------------------------------ #
# テスト用の最小 safety.yaml を一時ファイルで作る fixture
# ------------------------------------------------------------------ #

MINIMAL_CONFIG = {
    "blocklist": [
        "rm -rf /",
        "rm -rf ~",
        "sudo",
        "su ",
        "dd ",
        "mkfs",
        "> /dev/",
        "chmod 777 /",
    ],
    "dangerous": [
        "rm",
        "mv",
        "chmod",
        "chown",
        "kill",
        "killall",
        "systemctl stop",
        "systemctl disable",
    ],
    "allowed_paths": [
        "/home/john",
        "/tmp",
    ],
    "timeout": 30,
}


def make_checker(config: dict | None = None) -> tuple[SafetyChecker, tempfile.NamedTemporaryFile]:
    """一時設定ファイルから SafetyChecker を生成する。"""
    cfg = config if config is not None else MINIMAL_CONFIG
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    yaml.dump(cfg, tmp, allow_unicode=True)
    tmp.flush()
    return SafetyChecker(config_path=tmp.name), tmp


class TestBlocklist(unittest.TestCase):
    """チェック順序 1: blocklist → Level 3 / blocked=True"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _shell(self, command: str) -> dict:
        return {"tool": "shell", "command": command}

    def test_rm_rf_root_blocked(self):
        r = self.checker.check(self._shell("rm -rf /"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_rm_rf_home_blocked(self):
        r = self.checker.check(self._shell("rm -rf ~"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_sudo_blocked(self):
        r = self.checker.check(self._shell("sudo apt install vim"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_mkfs_blocked(self):
        r = self.checker.check(self._shell("mkfs.ext4 /dev/sda1"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_dev_redirect_blocked(self):
        r = self.checker.check(self._shell("echo x > /dev/sda"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)


class TestPathRestriction(unittest.TestCase):
    """チェック順序 2: allowed_paths 外 → Level 3 / blocked=True"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _fs(self, action: str, src: str, dst: str = "") -> dict:
        step = {"tool": "filesystem", "action": action, "src": src}
        if dst:
            step["dst"] = dst
        return step

    def test_src_outside_allowed_blocked(self):
        r = self.checker.check(self._fs("move", "/etc/passwd", "/tmp/"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_dst_outside_allowed_blocked(self):
        r = self.checker.check(self._fs("copy", "/home/john/file.txt", "/etc/"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_allowed_src_and_dst_passes(self):
        r = self.checker.check(self._fs("move", "/home/john/a.txt", "/tmp/"))
        self.assertFalse(r.blocked)

    def test_tmp_path_allowed(self):
        r = self.checker.check(self._fs("copy", "/tmp/foo.txt", "/home/john/"))
        self.assertFalse(r.blocked)

    def test_glob_src_allowed(self):
        r = self.checker.check(self._fs("move", "/home/john/Downloads/*.pdf", "/home/john/Documents/"))
        self.assertFalse(r.blocked)

    def test_glob_src_outside_blocked(self):
        r = self.checker.check(self._fs("move", "/var/log/*.log", "/tmp/"))
        self.assertTrue(r.blocked)


class TestDangerousPatterns(unittest.TestCase):
    """チェック順序 3: dangerous パターン → Level 2"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _shell(self, command: str) -> dict:
        return {"tool": "shell", "command": command}

    def test_rm_file_is_level2(self):
        r = self.checker.check(self._shell("rm /home/john/old.txt"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    def test_chmod_is_level2(self):
        r = self.checker.check(self._shell("chmod 644 /home/john/file.txt"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    def test_kill_is_level2(self):
        r = self.checker.check(self._shell("kill 1234"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    def test_systemctl_stop_is_level2(self):
        r = self.checker.check(self._shell("systemctl stop nginx"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)


class TestFilesystemLevels(unittest.TestCase):
    """チェック順序 4・5: filesystem操作 → Level 1 / 2"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _fs(self, action: str) -> dict:
        return {
            "tool": "filesystem",
            "action": action,
            "src": "/home/john/file.txt",
            "dst": "/tmp/",
        }

    def test_delete_is_level2(self):
        r = self.checker.check({
            "tool": "filesystem", "action": "delete", "src": "/home/john/file.txt"
        })
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)

    def test_move_is_level1(self):
        r = self.checker.check(self._fs("move"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_copy_is_level1(self):
        r = self.checker.check(self._fs("copy"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 1)

    def test_mkdir_is_level0(self):
        r = self.checker.check({
            "tool": "filesystem", "action": "mkdir", "src": "/home/john/newdir"
        })
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)


class TestReadOnlyLevel(unittest.TestCase):
    """チェック順序 6: 読み取り系 → Level 0"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _shell(self, command: str) -> dict:
        return {"tool": "shell", "command": command}

    def test_ls_is_level0(self):
        r = self.checker.check(self._shell("ls -la /home/john/Downloads"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_cat_is_level0(self):
        r = self.checker.check(self._shell("cat /home/john/notes.txt"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_find_is_level0(self):
        r = self.checker.check(self._shell("find /home/john -name '*.pdf'"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)


class TestBlocklistBeforePathCheck(unittest.TestCase):
    """blocklist は allowed_paths チェックより先に評価される。"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_sudo_in_allowed_path_still_blocked(self):
        """パスが allowed でも sudo は blocklist でブロックされる。"""
        step = {"tool": "shell", "command": "sudo rm /home/john/file.txt"}
        r = self.checker.check(step)
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)


class TestEmptyAllowedPaths(unittest.TestCase):
    """allowed_paths が空の場合は全パスを許可する。"""

    def setUp(self):
        cfg = dict(MINIMAL_CONFIG)
        cfg["allowed_paths"] = []
        self.checker, self.tmp = make_checker(cfg)

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_any_path_allowed_when_list_empty(self):
        r = self.checker.check({
            "tool": "filesystem", "action": "move",
            "src": "/etc/something", "dst": "/var/log/"
        })
        self.assertFalse(r.blocked)


class TestMvDangerous(unittest.TestCase):
    """mv コマンドが danger_level: 2 になることを確認。"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_mv_is_level2(self):
        r = self.checker.check({"tool": "shell", "command": "mv /home/john/Downloads/a.pdf /home/john/Documents/"})
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)


class TestShellPathRestriction(unittest.TestCase):
    """
    shell コマンド内のパス制限チェック（チェック順序 2）。

    filesystem ツールは src/dst フィールドで守られているが、
    shell は任意コマンドを実行できるため、コマンド文字列から
    パスを抽出して allowed_paths でチェックする。
    """

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _shell(self, command: str) -> dict:
        return {"tool": "shell", "command": command}

    # --- 許可されるケース ------------------------------------------ #

    def test_allowed_absolute_path_passes(self):
        """allowed_paths 内の絶対パスは通過する。"""
        r = self.checker.check(self._shell("cat /home/john/notes.txt"))
        self.assertFalse(r.blocked)

    def test_allowed_home_path_passes(self):
        """allowed_paths 内の絶対ホームパスは通過する。"""
        r = self.checker.check(self._shell("ls -la /home/john/Downloads"))
        self.assertFalse(r.blocked)

    def test_allowed_tmp_path_passes(self):
        """/tmp は allowed_paths に含まれるため通過する。"""
        r = self.checker.check(self._shell("cat /tmp/result.txt"))
        self.assertFalse(r.blocked)

    def test_no_path_in_command_passes(self):
        """パスを含まないコマンドはパスチェックの対象外。"""
        r = self.checker.check(self._shell("echo hello world"))
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 0)

    def test_find_in_allowed_dir_passes(self):
        """find の検索先が allowed_paths 内なら通過する。"""
        r = self.checker.check(self._shell("find /home/john -name '*.pdf' -mtime +30"))
        self.assertFalse(r.blocked)

    def test_glob_in_allowed_dir_passes(self):
        """グロブを含むパスが allowed_paths 内なら通過する。"""
        r = self.checker.check(self._shell("ls /home/john/Downloads/*.pdf"))
        self.assertFalse(r.blocked)

    def test_multiple_allowed_paths_passes(self):
        """コマンド内の複数パスがすべて allowed_paths 内なら通過する。"""
        r = self.checker.check(self._shell("cp /home/john/Downloads/a.txt /home/john/Documents/"))
        self.assertFalse(r.blocked)

    # --- ブロックされるケース -------------------------------------- #

    def test_disallowed_absolute_path_blocked(self):
        """/etc/passwd は allowed_paths 外なのでブロックされる。"""
        r = self.checker.check(self._shell("cat /etc/passwd"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_disallowed_var_path_blocked(self):
        """/var/log/ は allowed_paths 外なのでブロックされる。"""
        r = self.checker.check(self._shell("ls /var/log/syslog"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_redirect_to_disallowed_path_blocked(self):
        """リダイレクト先が allowed_paths 外ならブロックされる。"""
        r = self.checker.check(self._shell("echo hello > /etc/hosts"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_path_traversal_blocked(self):
        """パストラバーサル（/../）は resolve 後に allowed_paths 外と判定される。"""
        r = self.checker.check(self._shell("cat /home/john/../etc/passwd"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    def test_one_of_two_paths_disallowed_blocks(self):
        """複数パスのうち1つでも allowed_paths 外ならブロックされる。"""
        r = self.checker.check(self._shell("cp /home/john/file.txt /etc/"))
        self.assertTrue(r.blocked)
        self.assertEqual(r.danger_level, 3)

    # --- allowed_paths 空の場合 ------------------------------------ #

    def test_empty_allowed_paths_allows_any_shell_path(self):
        """allowed_paths が空の場合は shell のパスチェックも無効化される。"""
        cfg = dict(MINIMAL_CONFIG)
        cfg["allowed_paths"] = []
        tmp2 = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        yaml.dump(cfg, tmp2, allow_unicode=True)
        tmp2.flush()
        checker = SafetyChecker(config_path=tmp2.name)
        r = checker.check(self._shell("cat /etc/passwd"))
        self.assertFalse(r.blocked)
        Path(tmp2.name).unlink(missing_ok=True)


class TestExtractPathsFromCommand(unittest.TestCase):
    """_extract_paths_from_command の単体テスト。"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _extract(self, command: str) -> list[str]:
        return self.checker._extract_paths_from_command(command)

    def test_empty_command(self):
        self.assertEqual(self._extract(""), [])

    def test_absolute_path(self):
        self.assertIn("/etc/passwd", self._extract("cat /etc/passwd"))

    def test_home_path(self):
        self.assertIn("~/Downloads", self._extract("ls ~/Downloads"))

    def test_flags_excluded(self):
        paths = self._extract("ls -la /home/john")
        self.assertNotIn("-la", paths)
        self.assertIn("/home/john", paths)

    def test_no_path_command(self):
        self.assertEqual(self._extract("echo hello"), [])

    def test_redirect_path_extracted(self):
        paths = self._extract("echo x > /etc/hosts")
        self.assertIn("/etc/hosts", paths)

    def test_multiple_paths(self):
        paths = self._extract("cp ~/Downloads/a.txt ~/Documents/")
        self.assertIn("~/Downloads/a.txt", paths)
        self.assertIn("~/Documents/", paths)

    def test_quoted_path_with_space_is_known_limitation(self):
        """スペースを含むパスは完全対応しない（既知の限界）。スペース前まで抽出される。"""
        paths = self._extract("cat '/home/john/my file.txt'")
        # スペースで分割されるため "/home/john/my" まで抽出される
        self.assertIn("/home/john/my", paths)
        self.assertNotIn("/home/john/my file.txt", paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
