import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from native_sync import AGENTS, Lab
from probe_codex import Codex


def pair(user, answer):
    return [["user", user], ["assistant", answer]]


class NativeLabFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="awb-native-", dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.lab = Lab(Path(temp.name) / "store", create=True)
        self.a = pair("记住 A 的数字 18743。", "已记录 18743。")
        self.b = pair("记住 B 的数字 65391。", "已记录 65391。")
        self.a_codex = self.lab.seed("codex", "A", self.a)
        self.b_workbuddy = self.lab.seed("workbuddy", "B", self.b)

    def by_history(self, agent, messages):
        return next(item for item in self.lab.copies(agent).values() if item.messages == messages)

    def assert_histories(self, expected):
        for agent in AGENTS:
            histories = [item.messages for item in self.lab.copies(agent).values()]
            self.assertCountEqual(histories, expected)

    def assert_codex_native(self, expected):
        with Codex(self.lab.codex, self.lab.workspace) as app:
            listed = app.call("thread/list", {"limit": 20})["data"]
            self.assertEqual(len(listed), len(expected))
            for messages in expected:
                item = self.by_history("codex", messages)
                self.assertIn(item.sid, {thread["id"] for thread in listed})
                thread = app.call("thread/resume", {"threadId": item.sid})["thread"]
                self.assertEqual(len(thread["turns"]), len(messages) // 2)
                restored = []
                for turn in thread["turns"]:
                    self.assertEqual([entry["type"] for entry in turn["items"]], ["userMessage", "agentMessage"])
                    user, assistant = turn["items"]
                    restored.extend(pair(user["content"][0]["text"], assistant["text"]))
                self.assertEqual(restored, messages)


class NativeLabTest(NativeLabFixture):
    def test_workbuddy_native_context_is_not_replayed_in_codex(self):
        path = self.lab.wb_path(self.b_workbuddy)
        title, user, assistant = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        query = " 保留用户自己写的 <user_query>标记</user_query>。\n"
        user["providerData"] = {"startsNewUserRequest": True}
        user["content"][0]["text"] = (
            '<system-reminder data-role="user-context">WorkBuddy workspace instructions</system-reminder>\n'
            '<system-reminder data-role="additional-data">WorkBuddy clock</system-reminder>\n'
            f"<user_query>{query}</user_query>"
        )
        events = [
            title,
            {"type": "session-meta", "meta": {"codebuddy.ai/hostKind": "desktop"}},
            user,
            {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {}}},
            {"type": "reasoning", "content": [], "rawContent": [{"type": "reasoning_text", "text": "native reasoning"}]},
            assistant,
        ]
        original = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events)
        path.write_text(original, encoding="utf-8", newline="\n")
        expected = pair(query, self.b[1][1])
        self.lab.sync()
        self.assert_histories([self.a, expected])
        self.assert_codex_native([self.a, expected])
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.lab.sync()["unchanged"], 4)

    def test_workbuddy_unknown_context_and_tracked_files_are_not_imported(self):
        path = self.lab.wb_path(self.b_workbuddy)
        title, user, assistant = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        unknown = dict(user, providerData={"startsNewUserRequest": True},
                       content=[{"type": "input_text", "text": "<system-reminder>unknown</system-reminder>"}])
        attached = dict(unknown, content=[{"type": "input_text", "text": (
            '<system-reminder data-role="additional-data">'
            '<attachment_sandbox_paths>one.txt</attachment_sandbox_paths></system-reminder>\n'
            '<user_query>查看附件</user_query>'
        )}])
        snapshot = {"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {"one.txt": {}}}}
        for events, error in [
            ([title, unknown, assistant], "Unsupported WorkBuddy prompt context"),
            ([title, attached, assistant], "Unsupported WorkBuddy text attachment"),
            ([title, user, snapshot, assistant], "Unsupported model history"),
        ]:
            with self.subTest(error=error):
                path.write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error):
                    self.lab.sync()
                self.assertEqual(len(self.lab.copies("codex")), 1)

    def test_ab_union_native_codex_resume_and_bidirectional_append(self):
        self.assertEqual(self.lab.sync(), dict(sessions=2, branches=0, created=2, extended=0, unchanged=2))
        self.assert_histories([self.a, self.b])
        self.assert_codex_native([self.a, self.b])
        self.assertEqual(self.lab.sync()["created"], 0)

        wb = self.by_history("workbuddy", self.a)
        a_more = self.a + pair("再记住 90682。", "已记录 90682。")
        self.lab.publish("workbuddy", wb.sid, wb.title, a_more, wb)
        codex = self.by_history("codex", self.b)
        b_more = self.b + pair("现在追加 72810。", "已追加 72810。")
        self.lab.publish("codex", codex.sid, codex.title, b_more, codex)
        self.assertEqual(self.lab.sync()["extended"], 2)
        self.assert_histories([a_more, b_more])
        self.assert_codex_native([a_more, b_more])
        self.assertEqual(self.lab.sync(), dict(sessions=2, branches=0, created=0, extended=0, unchanged=4))

    def test_simultaneous_updates_of_one_conversation_create_branches(self):
        self.lab.sync()
        codex, wb = [self.by_history(agent, self.a) for agent in AGENTS]
        a1 = self.a + pair("Codex 分支", "保留 Codex 分支。")
        a2 = self.a + pair("WorkBuddy 分支", "保留 WorkBuddy 分支。")
        self.lab.publish("codex", codex.sid, codex.title, a1, codex)
        self.lab.publish("workbuddy", wb.sid, wb.title, a2, wb)
        self.assertEqual(self.lab.sync()["branches"], 1)
        self.assert_histories([a1, a2, self.b])
        self.assertEqual(self.lab.sync(), dict(sessions=3, branches=0, created=0, extended=0, unchanged=6))

    def test_interrupted_sync_reuses_projected_ids(self):
        publish = self.lab.publish
        successes = 0

        def fail_after_first_copy(*args):
            nonlocal successes
            if successes:
                raise OSError("fixture interruption")
            outcome = publish(*args)
            if outcome == "created":
                successes += 1
            return outcome
        with patch.object(self.lab, "publish", fail_after_first_copy):
            with self.assertRaisesRegex(OSError, "fixture interruption"):
                self.lab.sync()
        self.assertEqual(Lab(self.lab.root).sync()["created"], 1)
        self.assert_histories([self.a, self.b])

    def test_unsupported_turn_aborts_before_creating_replicas(self):
        item = self.by_history("codex", self.a)
        with item.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
        with self.assertRaisesRegex(ValueError, "Unsupported model history"):
            self.lab.sync()
        self.assertEqual(len(self.lab.copies("workbuddy")), 1)

    def test_overlapping_offline_sync_is_rejected(self):
        with self.lab.sync_lock():
            with self.assertRaisesRegex(RuntimeError, "already running"):
                Lab(self.lab.root).sync()
        self.assertEqual(self.lab.sync()["created"], 2)


if __name__ == "__main__":
    unittest.main()
