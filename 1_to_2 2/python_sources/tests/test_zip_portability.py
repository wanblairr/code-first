"""Portable Unicode ZIP header metadata and lossless extraction."""
from pathlib import Path
import stat
import struct
import tempfile
import unittest
import zipfile

from cafe_zip import archive_name, write_portable_zip


class ZipPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "中文目录"
        self.directory.mkdir()
        self.chinese = self.directory / "README_启动兼容性修复.md"
        self.chinese.write_text("吉隆坡天气、节假日与采购审批。\n", encoding="utf-8")
        self.ascii = self.root / "start.cmd"
        self.ascii.write_bytes(b"@echo off\r\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_unicode_file_and_directory_use_unix_modes_and_utf8_central_headers(self):
        target = write_portable_zip([
            (self.directory, "Cafe/中文目录"),
            (self.chinese, "Cafe/中文目录/README_启动兼容性修复.md"),
            (self.ascii, "Cafe/start.cmd"),
        ], self.root / "delivery.zip")
        with zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            for info in archive.infolist():
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr >> 16, (stat.S_IFDIR | 0o755) if info.is_dir() else (stat.S_IFREG | 0o644))
                self.assertNotIn("\\", info.filename)
                if not info.filename.isascii():
                    self.assertEqual(info.flag_bits & 0x800, 0x800)
            archive.extractall(self.root / "extracted")
        self.assertEqual((self.root / "extracted/Cafe/中文目录/README_启动兼容性修复.md").read_bytes(), self.chinese.read_bytes())

    def test_local_headers_have_matching_utf8_names_and_flags(self):
        name = "Cafe/中文目录/README_启动兼容性修复.md"
        target = write_portable_zip([(self.chinese, name)], self.root / "delivery.zip")
        raw = target.read_bytes()
        with zipfile.ZipFile(target) as archive:
            info = archive.getinfo(name)
            offset = info.header_offset
            self.assertEqual(raw[offset:offset + 4], b"PK\x03\x04")
            local_flags = struct.unpack_from("<H", raw, offset + 6)[0]
            length = struct.unpack_from("<H", raw, offset + 26)[0]
            encoded_name = raw[offset + 30:offset + 30 + length]
            self.assertEqual(local_flags & 0x800, 0x800)
            self.assertEqual(local_flags, info.flag_bits)
            self.assertEqual(encoded_name.decode("utf-8"), name)
            self.assertEqual(archive.read(name), self.chinese.read_bytes())

    def test_windows_archive_separators_are_normalized(self):
        target = write_portable_zip([(self.chinese, "Cafe\\中文目录\\说明.md")], self.root / "delivery.zip")
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(), ["Cafe/中文目录/说明.md"])

    def test_invalid_paths_and_duplicates_do_not_overwrite_existing_archive(self):
        target = self.root / "delivery.zip"
        target.write_bytes(b"keep existing delivery")
        invalid_names = ("/outside.md", "C:\\outside.md", "../outside.md", "Cafe/../outside.md", "Cafe//empty.md", "Cafe/./note.md", "")
        for name in invalid_names:
            with self.subTest(name=name), self.assertRaises(ValueError):
                write_portable_zip([(self.chinese, name)], target)
            self.assertEqual(target.read_bytes(), b"keep existing delivery")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            write_portable_zip([(self.chinese, "Cafe/说明.md"), (self.ascii, "Cafe\\说明.md")], target)
        self.assertEqual(target.read_bytes(), b"keep existing delivery")

    def test_missing_input_leaves_destination_and_parent_unchanged(self):
        target = self.root / "absent" / "delivery.zip"
        with self.assertRaises(ValueError):
            write_portable_zip([(self.root / "missing.txt", "Cafe/missing.txt")], target)
        self.assertFalse(target.parent.exists())


if __name__ == "__main__":
    unittest.main()
