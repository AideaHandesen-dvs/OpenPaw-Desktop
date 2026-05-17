"""
tests/test_filesystem.py - FilesystemTool の単体テスト
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.filesystem import copy, move, delete, mkdir


class TestFilesystemTool(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mkdir_creates_directory(self):
        target = str(self.base / "new_dir")
        result = mkdir(target)
        self.assertTrue(result.success)
        self.assertTrue(Path(target).is_dir())

    def test_mkdir_creates_nested(self):
        target = str(self.base / "a" / "b" / "c")
        result = mkdir(target)
        self.assertTrue(result.success)
        self.assertTrue(Path(target).is_dir())

    def test_mkdir_existing_ok(self):
        target = str(self.base / "existing")
        Path(target).mkdir()
        result = mkdir(target)
        self.assertTrue(result.success)

    def test_copy_single_file(self):
        src = self.base / "src.txt"
        src.write_text("hello")
        dst = str(self.base / "dst.txt")
        result = copy(str(src), dst)
        self.assertTrue(result.success)
        self.assertEqual(Path(dst).read_text(), "hello")

    def test_copy_into_directory(self):
        src = self.base / "file.txt"
        src.write_text("data")
        dst_dir = self.base / "dest"
        dst_dir.mkdir()
        result = copy(str(src), str(dst_dir))
        self.assertTrue(result.success)
        self.assertTrue((dst_dir / "file.txt").exists())

    def test_copy_glob(self):
        for i in range(3):
            (self.base / f"doc{i}.pdf").write_text(f"pdf{i}")
        dst_dir = self.base / "pdfs"
        dst_dir.mkdir()
        result = copy(str(self.base / "*.pdf"), str(dst_dir))
        self.assertTrue(result.success)
        self.assertEqual(len(list(dst_dir.glob("*.pdf"))), 3)

    def test_copy_nonexistent_fails(self):
        result = copy(str(self.base / "ghost.txt"), str(self.base / "out.txt"))
        self.assertFalse(result.success)

    def test_move_single_file(self):
        src = self.base / "original.txt"
        src.write_text("move me")
        dst = str(self.base / "moved.txt")
        result = move(str(src), dst)
        self.assertTrue(result.success)
        self.assertFalse(src.exists())
        self.assertTrue(Path(dst).exists())

    def test_move_glob_multiple_files(self):
        for i in range(3):
            (self.base / f"report{i}.pdf").write_text(f"r{i}")
        dst_dir = self.base / "archive"
        dst_dir.mkdir()
        result = move(str(self.base / "*.pdf"), str(dst_dir))
        self.assertTrue(result.success)
        self.assertEqual(result.output, "3 file(s) moved")
        self.assertEqual(len(list(dst_dir.glob("*.pdf"))), 3)

    def test_move_nonexistent_fails(self):
        result = move(str(self.base / "ghost.txt"), str(self.base / "dst.txt"))
        self.assertFalse(result.success)

    def test_delete_single_file(self):
        target = self.base / "todelete.txt"
        target.write_text("bye")
        result = delete(str(target))
        self.assertTrue(result.success)
        self.assertFalse(target.exists())

    def test_delete_directory(self):
        d = self.base / "subdir"
        d.mkdir()
        (d / "file.txt").write_text("inside")
        result = delete(str(d))
        self.assertTrue(result.success)
        self.assertFalse(d.exists())

    def test_delete_nonexistent_fails(self):
        result = delete(str(self.base / "ghost.txt"))
        self.assertFalse(result.success)

    def test_affected_paths_populated(self):
        for i in range(2):
            (self.base / f"f{i}.txt").write_text("x")
        dst_dir = self.base / "out"
        dst_dir.mkdir()
        result = move(str(self.base / "*.txt"), str(dst_dir))
        self.assertEqual(len(result.affected_paths), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
