"""Session readers and desktop handoff flow with isolated source logs."""
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import awb_sessions as S
from awb_session_panel import SessionPanel


def write_log(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.write("{unfinished")


def codex_msg(role, text, native_id):
    return {"type": "response_item", "timestamp": "2026-11-10T09:00:00Z",
            "payload": {"type": "message", "role": role, "id": native_id,
                        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


class LocalSessionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = root / "workspaces" / "电脑相关"
        self.project.mkdir(parents=True)
        sibling = self.project.with_name("电脑相关-其他")
        sibling.mkdir()
        self.codex, self.wb = root / "codex", root / "wb"
        events = [
            {"type": "session_meta", "payload": {"id": "c1", "cwd": str(self.project)}},
            codex_msg("user", "<environment_context>私有设置</environment_context>\n检查中文问题", "user1"),
            codex_msg("assistant", "可在离线模式复现", "assistant1"),
            codex_msg("developer", "不应进入接力包", "developer1"),
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "工具输出不应进入"}},
        ]
        self.codex_log = self.codex / "sessions" / "2026" / "rollout-c1.jsonl"
        write_log(self.codex_log, events)
        write_log(self.codex / "archived_sessions" / "old-copy.jsonl", events)
        write_log(self.codex / "sessions" / "other.jsonl",
                  [{"type": "session_meta", "payload": {"id": "other", "cwd": str(sibling)}},
                   codex_msg("user", "隔壁项目", "user2")])
        write_log(self.codex / "session_index.jsonl", [{"id": "c1", "thread_name": "自定义标题"}])
        self.wb_log = self.wb / "projects" / "slug" / "one.jsonl"
        write_log(self.wb_log, [
            {"type": "ai-title", "sessionId": "w1", "cwd": str(self.project), "aiTitle": "WorkBuddy 记录"},
            {"type": "message", "sessionId": "w1", "role": "assistant", "content": "核对接力步骤"},
        ])

    def scan(self, project):
        return S.scan(project, self.codex, self.wb)

    def test_project_isolation_deduplication_and_search(self):
        items, errors = self.scan(self.project)
        self.assertEqual(errors, 0)
        self.assertEqual({item.id for item in items}, {"c1", "w1"})
        codex = next(item for item in items if item.id == "c1")
        self.assertEqual(codex.title, "自定义标题")
        self.assertEqual(len(codex.files), 2)
        self.assertEqual([msg.text for msg in S.messages(codex)], ["检查中文问题", "可在离线模式复现"])
        hits, errors = S.search(items, "离线模式")
        self.assertEqual([(item.id, anchor) for item, anchor in hits], [("c1", "正文 · 第 3 行")])
        self.assertEqual(errors, 0)
        self.assertEqual(S.search(items, "工具输出")[0], [])
        self.assertEqual(S.search(items, "隔壁项目")[0], [])

    def test_gui_can_copy_an_edited_handoff_with_sources(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        panel = SessionPanel(root, self.project.parent, [self.project.name])
        panel.scope.set(self.project.name)
        before = self.wb_log.read_bytes()

        def wait():
            deadline = time.monotonic() + 5
            while panel.busy and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertFalse(panel.busy)
            root.update()

        scan = S.scan
        with patch.object(S, "scan", lambda project: scan(project, self.codex, self.wb)):
            panel.load()
            wait()
        panel.keyword.set("核对接力步骤")
        panel.find()
        wait()
        self.assertEqual(len(panel.rows), 1)
        panel.tree.selection_set("0")
        root.update()
        wait()
        panel.prepare()
        self.assertTrue(panel.pack_ready)
        panel.text.insert("end", "\n请验证接收端。")
        with patch.object(panel, "clipboard_clear"), patch.object(panel, "clipboard_append") as clipboard:
            panel.copy_pack()
        draft = clipboard.call_args.args[0]
        self.assertIn("one.jsonl 第 2 行", draft)
        self.assertIn("> 核对接力步骤", draft)
        self.assertIn("请验证接收端。", draft)
        self.assertEqual(self.wb_log.read_bytes(), before)
