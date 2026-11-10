from contextlib import closing
import sqlite3
from unittest.mock import patch
import zipfile

from backup_lab import capture, verify
from offline_sync import OfflineSync
from test_native_sync import NativeLabFixture, pair


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class OfflineCases(NativeLabFixture):
    def test_waits_for_both_exits_and_last_disk_write_before_launch(self):
        clock = Clock()
        active = {"codex", "workbuddy"}
        gate = OfflineSync(self.lab, lambda: active, 0.5, clock, clock.sleep)
        gate.request()
        self.assertEqual(gate.tick()[0], "waiting")
        active.remove("codex")
        self.assertEqual(gate.tick()[0], "waiting")
        active.remove("workbuddy")
        self.assertEqual(gate.tick()[0], "settling")
        clock.sleep(0.3)
        current = self.by_history("workbuddy", self.b)
        newer = self.b + pair("最后落盘的消息", "已保存")
        self.lab.publish("workbuddy", current.sid, current.title, newer, current)
        self.assertEqual(gate.tick()[0], "settling")
        clock.sleep(0.3)
        self.assertEqual(gate.tick()[0], "settling")
        self.assertEqual(len(self.lab.copies("codex")), 1)
        clock.sleep(0.3)
        self.assertEqual(gate.tick()[0], "synced")
        self.assert_histories([self.a, newer])

        def reopen():
            self.assert_histories([self.a, newer])
            active.add("codex")
            return "launched"
        launch, result = gate.launch_after_sync(reopen)
        self.assertEqual(launch, "launched")
        self.assertEqual(result["created"], 0)

    def test_reopen_during_sync_stops_remaining_writes_and_retries(self):
        clock = Clock()
        active = set()
        gate = OfflineSync(self.lab, lambda: active, 0, clock, clock.sleep)
        gate.request()
        self.assertEqual(gate.tick()[0], "settling")
        publish = self.lab.publish
        def reopen(*args):
            result = publish(*args)
            if result == "created":
                active.add("workbuddy")
            return result
        with patch.object(self.lab, "publish", reopen):
            self.assertEqual(gate.tick()[0], "waiting")
        self.assertTrue(gate.pending)
        self.assertEqual(sum(len(self.lab.copies(a)) for a in ("codex", "workbuddy")), 3)
        active.clear()
        self.assertEqual(gate.tick()[0], "settling")
        self.assertEqual(gate.tick()[0], "synced")
        self.assert_histories([self.a, self.b])

    def test_backup_contains_uncheckpointed_commits_and_no_auth_files(self):
        self.lab.local("codex", "auth.json").write_text("synthetic credential", encoding="utf-8")
        self.lab.local("workbuddy", "settings.json").write_text("synthetic setting", encoding="utf-8")
        with closing(sqlite3.connect(self.lab.wb_db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE sessions SET title='Committed in WAL' WHERE id=?", (self.b_workbuddy,))
            writer.commit()
            self.assertGreater(self.lab.wb_db.with_name("workbuddy.db-wal").stat().st_size, 0)
            path = self.lab.local("before.zip")
            manifest = capture(self.lab, path)
        self.assertEqual(verify(path), manifest)
        self.assertNotIn("codex/auth.json", manifest)
        self.assertNotIn("workbuddy/settings.json", manifest)
        self.assertTrue(any(name.startswith("codex/sessions/") for name in manifest))
        self.assertTrue(any(name.startswith("workbuddy/projects/") for name in manifest))
        extracted = self.lab.local("verified-copy.sqlite")
        with zipfile.ZipFile(path) as archive:
            extracted.write_bytes(archive.read("workbuddy/workbuddy.db"))
        with closing(sqlite3.connect(extracted)) as restored:
            self.assertEqual(restored.execute("SELECT title FROM sessions").fetchone(), ("Committed in WAL",))
