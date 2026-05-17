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


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestMvDangerous(unittest.TestCase):
    """mv コマンドが danger_level: 2 になることを確認。"""

    def setUp(self):
        self.checker, self.tmp = make_checker()

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_mv_is_level2(self):
        r = self.checker.check({"tool": "shell", "command": "mv ~/Downloads/a.pdf ~/Documents/"})
        self.assertFalse(r.blocked)
        self.assertEqual(r.danger_level, 2)
