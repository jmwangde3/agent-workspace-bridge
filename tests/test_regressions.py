import datetime
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import awb_gui as G
import awb_links as L
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
