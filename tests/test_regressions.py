import datetime
import io
import json
from pathlib import Path
import struct
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
import zipfile
import zlib

import awb_gui as G
import awb_links as L
import awb_memory_backup as B
import share_workspace as W


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "w1").mkdir()

    def build(self):
        return W.build(str(self.root / "w1"))

    def test_memory_index_today_and_nested_notes_survive(self):
        original = self.root / 'w1' / '.workbuddy' / 'memory'
        (original / 'nested').mkdir(parents=True)
        today = datetime.date.today().isoformat() + '.md'
        for name in ('MEMORY.md', today, 'nested/archive.md'):
            (original / name).write_text('original:' + name, encoding='utf-8')
        self.build()
        for name in ('MEMORY.md', today, 'nested/archive.md'):
            self.assertEqual((original / name).read_text(encoding='utf-8'), 'original:' + name)
        self.assertEqual(len(list(original.parent.glob('memory.bak-*'))), 1)

    def test_memory_conflict_keeps_both_versions(self):
        ws = self.root / 'w1'
        source, target = ws / '.workbuddy' / 'memory', ws / '.shared' / 'memory'
        source.mkdir(parents=True); target.mkdir(parents=True)
        (source / 'MEMORY.md').write_text('source', encoding='utf-8')
        (target / 'MEMORY.md').write_text('canonical', encoding='utf-8')
        self.build()
        self.assertEqual((target / 'MEMORY.md').read_text(encoding='utf-8'), 'canonical')
        self.assertEqual(next(target.glob('MEMORY.md.imported-*')).read_text(encoding='utf-8'), 'source')

    def test_failed_memory_copy_keeps_original_active(self):
        source = self.root / 'w1' / '.workbuddy' / 'memory'
        source.mkdir(parents=True)
        (source / 'MEMORY.md').write_text('original', encoding='utf-8')
        with patch.object(W.shutil, 'copy2', side_effect=OSError('copy failure')):
            with self.assertRaises(OSError):
                self.build()
        self.assertFalse(L.is_link(str(source)))
        self.assertEqual((source / 'MEMORY.md').read_text(encoding='utf-8'), 'original')


    def test_memory_build_leaves_existing_agent_folder_intact(self):
        ws = self.root / "w1"
        foreign = ws / ".codex" / "skills" / "existing"
        foreign.mkdir(parents=True)
        artifact = foreign / "SKILL.md"
        artifact.write_text("externally managed fixture", encoding="utf-8")
        self.build()
        self.assertEqual(W.check(str(ws)), 0)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "externally managed fixture")
        self.assertFalse((ws / ".shared" / "skills").exists())

    def test_console_sync_uses_selected_workspaces_and_preview_is_read_only(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with patch.object(G.C, "WORKSPACE_ROOT", str(self.root)), patch.object(G.App, "_init_logging"):
            app = G.App(root)
            app.sync_list.selection_set(0)
            self.assertEqual(len(app.nb.tabs()), 4)
            with patch.object(app.runner, "run") as run, patch.object(G.subprocess, "Popen") as spawn:
                app.sync_action("preview")
                self.assertEqual(run.call_args.args[1][-3:], ["preview", str(self.root / "w1"), "--compatible-only"])
                spawn.assert_not_called()
                app.sync_action("monitor")
                self.assertEqual(spawn.call_args.args[0][-3:], ["monitor", str(self.root / "w1"), "--compatible-only"])
                app.sync_action("diagnose")
                result = run.call_args.kwargs
                result["private_line"]("WORKSPACE " + str(self.root / "w1"))
                result["private_line"]('RECOVERY_REQUIRED {"id": "test"}')
                result["after"]()
                self.assertTrue(all(button.instate(["disabled"]) for button in app.sync_writes))
                app.sync_action("diagnose")
                result = run.call_args.kwargs
                result["private_line"]("WORKSPACE " + str(self.root / "w1"))
                result["private_line"]("SCHEMA CODEX READ_WRITE_SUPPORTED")
                result["after"]()
                self.assertTrue(all(button.instate(["!disabled"]) for button in app.sync_writes))

    def test_memory_backup_preview_recovery_and_merge_preserve_content(self):
        ws = self.root / "w1"
        self.build()
        memory = ws / ".shared" / "memory"
        note = memory / "MEMORY.md"
        note.write_text("original", encoding="utf-8")
        nested = memory / "nested"
        nested.mkdir()
        lost = nested / "lost.md"
        lost.write_text("recover me", encoding="utf-8")
        archive = B.backup(ws, memory)
        note.write_text("new content", encoding="utf-8")
        lost.unlink()
        self.assertIsNone(B.recover(ws, memory, archive, preview=True))
        self.assertFalse(list(memory.parent.glob("recovered-memory-*")))
        recovered = B.recover(ws, memory, archive)
        self.assertEqual((recovered / "nested" / lost.name).read_text(encoding="utf-8"), "recover me")
        self.assertEqual(note.read_text(encoding="utf-8"), "new content")
        for _ in range(2):
            W.restore_memory(str(ws), archive, preview=False, apply=True)
        self.assertEqual(lost.read_text(encoding="utf-8"), "recover me")
        self.assertEqual(note.read_text(encoding="utf-8"), "new content")
        conflicts = list(memory.glob("MEMORY.md.restored-*"))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].read_text(encoding="utf-8"), "original")
        self.assertEqual(W.check(str(ws)), 0)

    def test_memory_restore_rejects_traversal_links_and_bad_hash_before_recovery(self):
        ws = self.root / "w1"
        self.build()
        memory = ws / ".shared" / "memory"
        for name, mode in (("../outside", 0), ("memory/alias", 0o120000), ("memory/tampered", 0)):
            with self.subTest(name=name):
                archive = self.root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as file:
                    entry = zipfile.ZipInfo(name)
                    entry.external_attr = mode << 16
                    file.writestr(entry, b"data")
                    file.writestr("manifest.json", json.dumps({
                        "version": 1, "workspace": L.normalized(ws),
                        "files": {"tampered": B.digest(io.BytesIO(b"expected"))},
                    }))
                with self.assertRaises(ValueError):
                    B.recover(ws, memory, archive)
                self.assertFalse(list(memory.parent.glob("recovered-memory-*")))

    def test_all_status_and_check_cover_uninitialized_workspace_and_build_initializes_it(self):
        ws = self.root / "w1"
        with patch.object(W.C, "WORKSPACE_ROOT", str(self.root)):
            for flags, expected in ((["--status"], 0), (["--check"], 1), (["--probe"], 0), ([], 0)):
                with patch("sys.argv", ["share_workspace.py", "--all", *flags]):
                    self.assertEqual(W.main(), expected)
        self.assertEqual(W.check(str(ws)), 0)

    def test_archive_budgets_reject_restore_and_backup_without_publishing(self):
        ws = self.root / "w1"
        self.build()
        memory = ws / ".shared" / "memory"
        for index in range(2):
            (memory / f"fixture-{index}").write_bytes(b"x" * 20)
        archive = B.backup(ws, memory)
        folder = archive.parent
        for field, limit in (("MAX_FILES", 1), ("MAX_FILE_SIZE", 19), ("MAX_TOTAL_SIZE", 30), ("MAX_ZIP_SIZE", 32),
                             ("MAX_MANIFEST_SIZE", 32)):
            with self.subTest(field=field), patch.object(B, field, limit):
                with self.assertRaises(ValueError):
                    B.recover(ws, memory, archive)
                with self.assertRaises(ValueError):
                    B.backup(ws, memory)
                self.assertEqual(list(folder.iterdir()), [archive])
                self.assertFalse(list(memory.parent.glob("*recover*")))

    def test_archive_rejects_false_central_size_and_case_duplicates(self):
        ws = self.root / "w1"
        self.build()
        memory = ws / ".shared" / "memory"
        path = self.root / "invalid.zip"
        name = "memory/sample.md"
        identity = B.digest(io.BytesIO(b"x"))
        manifest = {"version": 1, "workspace": L.normalized(ws), "files": {"sample.md": identity}}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, b"x" * 100)
            archive.writestr("manifest.json", json.dumps(manifest))
        data = bytearray(path.read_bytes())
        start = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, start + 16, zlib.crc32(b"x"))
        struct.pack_into("<I", data, start + 24, 1)
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "decoded beyond"):
            B.recover(ws, memory, path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name, b"x")
            archive.writestr(name.upper(), b"x")
            archive.writestr("manifest.json", json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Unsupported archive entry"):
            B.recover(ws, memory, path)
        self.assertFalse(list(memory.parent.glob("*recover*")))

    def test_interrupted_extraction_leaves_no_finished_recovery(self):
        ws = self.root / "w1"
        self.build()
        memory = ws / ".shared" / "memory"
        note = memory / "MEMORY.md"
        before = note.read_bytes()
        archive = B.backup(ws, memory)
        original = B.digest_chunks

        def interrupt(chunks, output=None, limit=None):
            if output is not None and not isinstance(output, io.BytesIO):
                output.write(b"partial")
                raise KeyboardInterrupt
            return original(chunks, output, limit)

        with patch.object(B, "digest_chunks", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            B.recover(ws, memory, archive)
        self.assertEqual(note.read_bytes(), before)
        self.assertFalse(list(memory.parent.glob("*recover*")))
