"""Exercise the opt-in gate with native file layouts in a private test directory."""

from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from backup_lab import verify
import codex_rollout as cx
from native_sync import Lab, OWNER, read_messages, stable
from offline_sync import ClientActive, OfflineSync
from probe_codex import Codex
from test_native_sync import pair
from test_offline_sync import Clock
from workspace_sync import WorkspaceBatch, WorkspaceSync, report_results, run_cycles


class WorkspaceGateCases(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="awb-workspace-", dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.lab = Lab(Path(temp.name) / "store", create=True)
        self.a = pair("A 314159", "A 314159")
        self.b = pair("B 271828", "B 271828")
        self.a_id = self.lab.seed("codex", "A", self.a)
        self.b_id = self.lab.seed("workbuddy", "B", self.b)
        self.a_path = self.lab.copies("codex")[self.a_id].path
        first, _, tail = self.a_path.read_text(encoding="utf-8").partition("\n")
        header = json.loads(first)
        header["payload"].update(source="vscode", originator="Codex Desktop", thread_source="user")
        self.a_path.write_text(cx.encode([header]) + tail, encoding="utf-8", newline="\n")
        self.b_cwd = str(self.lab.workspace)
        if os.name == "nt":
            self.b_cwd = self.b_cwd[0].lower() + self.b_cwd[1:]
        self.outside = self.lab.root / "another-workspace"
        self.outside.mkdir()
        self.outside_codex = cx.create(self.lab.codex, "33333333-3333-4333-8333-333333333333",
                                       self.outside, pair("Outside", "Outside answer"), time.time())
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET cwd=? WHERE id=?", (self.b_cwd, self.b_id))
            db.execute("INSERT INTO sessions (id,cwd,user_id,created_at,updated_at,status) VALUES (?,?,?,?,?,?)",
                       ("outside", str(self.outside), OWNER, 0, 0, "completed"))
        with Codex(self.lab.codex, self.lab.workspace) as client:
            self.assertEqual(len(client.call("thread/list", {"limit": 10})["data"]), 2)
        self.accounts = ("test-codex-account", OWNER)
        self.store = WorkspaceSync(self.lab.workspace, self.lab.codex, self.lab.workbuddy,
                                   Path(temp.name) / "state", lambda: self.accounts)

    def test_desktop_page_context_is_skipped_but_tool_results_are_blocked(self):
        path = cx.create(self.lab.codex, "44444444-4444-4444-8444-444444444444", self.lab.workspace,
                         self.a, time.time(), history_mode="paginated")
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        turn = events[1]["payload"]["turn_id"]
        pages = []
        for name in ("codex_apps_open_page", "codex_apps_selected_page_part_1_of_2",
                     "codex_apps_selected_page_part_2_of_2"):
            page = cx.event(time.time(), "response_item", {
                "type": "function_call_output", "name": name, "namespace": "agent",
                "output": "desktop page context fixture",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn},
            })
            page["metadata"] = {"client_authored": False}
            pages.append(page)
        events[2:2] = pages
        for ordinal, event in enumerate(events):
            event["ordinal"] = ordinal
        path.write_text(cx.encode(events), encoding="utf-8")
        self.assertEqual(read_messages(path, "codex"), self.a)
        pages[0]["payload"]["call_id"] = "actual-tool-call"
        path.write_text(cx.encode(events), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsupported Codex model item"):
            read_messages(path, "codex")

    def test_preview_and_close_merge_backup_scope_and_replay(self):
        for agent in ("codex", "workbuddy"):
            self.assertEqual(len(self.store.copies(agent)), 1)
        self.assertFalse(self.store.root.exists())
        clock = Clock()
        active = {"Codex", "WorkBuddy"}
        gate = OfflineSync(self.store, lambda: active, 1, clock, clock.sleep)
        gate.request()
        self.assertEqual(gate.tick()[0], "waiting")
        active.remove("Codex")
        self.assertEqual(gate.tick()[0], "waiting")
        active.clear()
        self.assertEqual(gate.tick()[0], "settling")
        clock.sleep(1)
        status, result = gate.tick()
        self.assertEqual(status, "synced")
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["codex_indexed"], 1)
        self.assertIn("codex/state_5.sqlite", verify(self.store.last_backup))
        self.assertFalse(any(name.endswith(self.outside_codex.name) for name in verify(self.store.last_backup)))
        with zipfile.ZipFile(self.store.last_backup) as archive:
            paths = archive.namelist()
            self.assertFalse(any(name.endswith("auth.json") for name in paths))
            self.assertEqual(len([p for p in paths if "/projects/" in p or "/sessions/" in p]), 2)
        self.assertEqual(set(self.store.copies("workbuddy")), {self.b_id, next(
            sid for sid, copy in self.store.copies("workbuddy").items() if copy.messages == self.a
        )})
        with closing(sqlite3.connect(self.lab.wb_db)) as db:
            cwds = db.execute("SELECT DISTINCT cwd FROM sessions WHERE id<>'outside'").fetchall()
            self.assertEqual(cwds, [(self.b_cwd,)])
        with Codex(self.lab.codex, self.lab.workspace) as client:
            listed = client.call("thread/list", {"limit": 10, "useStateDbOnly": True, "sourceKinds": ["vscode"]})["data"]
            replicas = self.store.copies("codex")
            self.assertEqual(len(replicas), 2)
            self.assertEqual(len(listed), 2)
            for sid, copy in replicas.items():
                self.assertIn(sid, {row["id"] for row in listed})
                thread = client.call("thread/resume", {"threadId": sid})["thread"]
                if thread.get("historyMode") == "paginated":
                    turn, = client.call("thread/turns/list", {"threadId": sid})["data"]
                    self.assertIn(copy.messages[0][1], json.dumps(
                        client.call("thread/items/list", {"threadId": sid, "turnId": turn["id"]})["data"]
                    ))
                else:
                    self.assertIn(copy.messages[0][1], json.dumps(thread["turns"]))
        wb_b = self.store.copies("workbuddy")[self.b_id]
        extended = self.b + pair("B continued", "B complete")
        self.lab.publish("workbuddy", self.b_id, "B", extended, wb_b)
        self.assertEqual(self.store.sync(lambda: None)["extended"], 1)
        codex_b = next(item for item in self.store.copies("codex").values() if item.sid != self.a_id)
        self.assertEqual(codex_b.messages, extended)
        self.assertEqual(self.store.sync(lambda: None)["unchanged"], 4)

    def test_repairs_previous_replica_missing_from_desktop_index(self):
        self.store.sync(lambda: None)
        b = next(item for item in self.store.copies("codex").values() if item.sid != self.a_id)
        a_before = self.a_path.read_bytes()
        first, _, tail = b.path.read_text(encoding="utf-8").partition("\n")
        header = json.loads(first)
        header["payload"].update(source="cli", originator="codex_cli_rs")
        b.path.write_text(cx.encode([header]) + tail, encoding="utf-8", newline="\n")
        with closing(sqlite3.connect(self.lab.codex / "state_5.sqlite")) as db, db:
            db.execute("DELETE FROM threads WHERE id=?", (b.sid,))
            if os.name == "nt":
                db.execute("UPDATE threads SET cwd=?, rollout_path=? WHERE id=?",
                           ("\\\\?\\" + str(self.lab.workspace), "\\\\?\\" + str(self.a_path), self.a_id))
        with closing(sqlite3.connect(self.store.state)) as db, db:
            db.execute("DROP TABLE managed_codex")
        result = self.store.sync(lambda: None)
        self.assertEqual((result["created"], result["unchanged"], result["codex_indexed"]), (0, 4, 1))
        self.assertEqual(self.a_path.read_bytes(), a_before)
        self.assertEqual(read_messages(b.path, "codex"), self.b)
        with Codex(self.lab.codex, self.lab.workspace) as client:
            rows = client.call("thread/list", {"useStateDbOnly": True, "sourceKinds": ["vscode"]})["data"]
            self.assertEqual({row["id"] for row in rows}, {self.a_id, b.sid})
            thread = client.call("thread/resume", {"threadId": b.sid})["thread"]
            self.assertEqual(thread["id"], b.sid)

    def test_monitor_merges_multiple_close_cycles_and_skips_unchanged_backup(self):
        clock = Clock()
        active = {"Codex", "WorkBuddy"}
        gate = OfflineSync(self.store, lambda: active, 0, clock, clock.sleep)
        a_more = self.a + pair("Codex second round", "Codex second reply")
        b_more = self.b + pair("WorkBuddy second round", "WorkBuddy second reply")

        def second_round():
            active.update(("Codex", "WorkBuddy"))
            a = self.store.copies("codex")[self.a_id]
            b = self.store.copies("workbuddy")[self.b_id]
            self.lab.publish("codex", self.a_id, "A", a_more, a)
            self.lab.publish("workbuddy", self.b_id, "B", b_more, b)

        steps = iter((active.clear, None, second_round, active.clear, None,
                      lambda: active.add("Codex"), active.clear, None))

        def pause(seconds):
            clock.sleep(seconds)
            action = next(steps)
            if action is not None:
                action()

        output = StringIO()
        with redirect_stdout(output), self.assertRaises(StopIteration):
            run_cycles(self.store, gate, True, pause)
        runs = [json.loads(line.removeprefix("SYNC_COMPLETE ")) for line in output.getvalue().splitlines()
                if line.startswith("SYNC_COMPLETE ")]
        self.assertEqual([(r["created"], r["extended"], r["unchanged"]) for r in runs],
                         [(2, 0, 2), (0, 2, 2), (0, 0, 4)])
        self.assertEqual(output.getvalue().count("VERIFIED_BACKUP "), 2)
        self.assertEqual(len(list(self.store.root.glob("before-*.zip"))), 2)
        for agent in ("codex", "workbuddy"):
            self.assertCountEqual([item.messages for item in self.store.copies(agent).values()], [a_more, b_more])

    def test_monitor_retries_transient_sqlite_lock(self):
        clock = Clock()
        gate = OfflineSync(self.store, lambda: (), 0, clock, clock.sleep)
        sync = self.store.sync
        attempts = 0

        def temporary_lock(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return sync(*args, **kwargs)

        output = StringIO()
        with patch.object(self.store, "sync", temporary_lock), redirect_stdout(output):
            run_cycles(self.store, gate, False, clock.sleep)
        self.assertEqual(attempts, 2)
        self.assertIn("SYNC_RETRY sqlite_busy 1", output.getvalue())
        self.assertIn("SYNC_COMPLETE", output.getvalue())

    def test_batch_isolates_blocked_workspace_and_gates_launch(self):
        other = WorkspaceSync(self.outside, self.lab.codex, self.lab.workbuddy,
                              self.store.root.with_name("outside-state"), lambda: self.accounts)
        batch = WorkspaceBatch([self.store, other])
        clock = Clock()
        gate = OfflineSync(batch, lambda: (), 0, clock, clock.sleep)
        launch = Mock()
        first = StringIO()
        with redirect_stdout(first):
            self.assertFalse(run_cycles(batch, gate, False, clock.sleep, launch))
        self.assertIn("SYNC_COMPLETE", first.getvalue())
        self.assertIn("WORKSPACE_BLOCKED", first.getvalue())
        self.assertIn("LAUNCH_SKIPPED", first.getvalue())
        launch.assert_not_called()
        self.assertEqual(len(self.store.copies("codex")), 2)

        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("DELETE FROM sessions WHERE id='outside'")
        other_lab = Lab(self.lab.root)
        other_lab.workspace = self.outside
        other_lab.seed("workbuddy", "Other B", pair("Other B", "Other answer"))

        def verify_ready():
            for store in (self.store, other):
                self.assertEqual(len(store.copies("codex")), 2)
                self.assertEqual(len(store.copies("workbuddy")), 2)

        launch.side_effect = verify_ready
        second = StringIO()
        with redirect_stdout(second):
            self.assertTrue(run_cycles(batch, gate, False, clock.sleep, launch))
        launch.assert_called_once_with()
        self.assertEqual(second.getvalue().count("SYNC_COMPLETE "), 2)
        self.assertIn("LAUNCH_REQUESTED", second.getvalue())
        self.assertEqual(len(list(self.store.root.glob("before-*.zip"))), 1)

    def test_launch_waits_if_a_client_reopens_before_start(self):
        clock = Clock()
        active = set()
        gate = OfflineSync(self.store, lambda: active, 0, clock, clock.sleep)
        checks = 0

        def reopen_once(store, result):
            nonlocal checks
            checks += 1
            if checks == 1:
                active.add("Codex")
            return report_results(store, result)

        def pause(seconds):
            active.clear()
            clock.sleep(seconds)

        launch = Mock(side_effect=lambda: self.assertEqual(checks, 2))
        with patch("workspace_sync.report_results", reopen_once), redirect_stdout(StringIO()):
            self.assertTrue(run_cycles(self.store, gate, False, pause, launch))
        launch.assert_called_once_with()
        self.assertEqual(len(list(self.store.root.glob("before-*.zip"))), 1)

    def test_compatible_only_defers_unsupported_sessions_without_copying_them(self):
        extra_codex = self.lab.seed("codex", "Native tool", pair("Native tool", "Tool answer"))
        bad_codex = next((self.lab.codex / "sessions").rglob("rollout-*" + extra_codex + ".jsonl"))
        extra_wb = self.lab.seed("workbuddy", "Native file", pair("Native file", "File answer"))
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET cwd=? WHERE id=?", (self.b_cwd, extra_wb))
        bad_wb = self.lab.wb_path(extra_wb)
        with bad_codex.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
        with bad_wb.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "tool-call"}) + "\n")
        original = {path: path.read_bytes() for path in (bad_codex, bad_wb)}
        self.store.compatible_only = True
        native, groups, deferred = self.store.selection()
        self.assertEqual([len(v) for v in native.values()], [1, 1])
        self.assertFalse(groups)
        self.assertEqual(self.store.deferred_counts(deferred),
                         {"codex": {"unsupported_history": 1}, "workbuddy": {"unsupported_history": 1}})
        result = self.store.sync(lambda: None)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["deferred_groups"], 0)
        for path, before in original.items():
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(any(name.endswith(path.name) for name in verify(self.store.last_backup)))

    def test_mapped_group_stays_frozen_while_another_group_advances(self):
        self.store.sync(lambda: None)
        wb = self.store.copies("workbuddy")
        a_wb = next(item for item in wb.values() if item.sid != self.b_id)
        a_more = self.a + pair("A local follow-up", "A local reply")
        b_more = self.b + pair("B local follow-up", "B local reply")
        self.lab.publish("workbuddy", a_wb.sid, a_wb.title, a_more, a_wb)
        self.lab.publish("workbuddy", self.b_id, "B", b_more, wb[self.b_id])
        clean = self.a_path.read_bytes()
        with self.a_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
        unsafe = self.a_path.read_bytes()
        a_wb_before = a_wb.path.read_bytes()
        self.store.compatible_only = True
        result = self.store.sync(lambda: None)
        self.assertEqual((result["sessions"], result["extended"], result["deferred_groups"]), (1, 1, 1))
        self.assertEqual(result["deferred"]["workbuddy"], {"counterpart_deferred": 1})
        self.assertEqual(self.a_path.read_bytes(), unsafe)
        self.assertEqual(a_wb.path.read_bytes(), a_wb_before)
        result = self.store.sync(lambda: None)
        self.assertEqual((result["unchanged"], result["deferred_groups"]), (2, 1))
        self.assertIsNone(self.store.last_backup)
        self.a_path.write_bytes(clean)
        result = self.store.sync(lambda: None)
        self.assertEqual((result["created"], result["extended"]), (0, 1))
        self.assertEqual(read_messages(self.a_path, "codex"), a_more)

    def test_compatible_only_can_import_with_a_deferred_codex_template(self):
        with self.a_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
        before = self.a_path.read_bytes()
        self.store.compatible_only = True
        result = self.store.sync(lambda: None)
        self.assertEqual((result["sessions"], result["created"], result["codex_indexed"]), (1, 1, 1))
        self.assertEqual(self.a_path.read_bytes(), before)
        native, _, _ = self.store.selection()
        self.assertEqual([item.messages for item in native["codex"].values()], [self.b])

    def test_compatible_only_does_not_revive_a_hidden_unfinished_projection(self):
        self.store.sync(lambda: None)
        with closing(sqlite3.connect(self.store.state)) as db, db:
            db.execute("DELETE FROM published WHERE agent='workbuddy' AND native=?", (self.b_id,))
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET status='archived' WHERE id=?", (self.b_id,))
        self.store.compatible_only = True
        result = self.store.sync(lambda: None)
        self.assertEqual((result["created"], result["deferred_groups"]), (0, 1))
        self.assertEqual(result["deferred"]["workbuddy"], {"mapped_hidden": 1})
        self.assertIsNone(self.store.last_backup)

    def test_compatible_only_reserves_deferred_native_ids(self):
        projected_id = stable(stable(self.b_id, "AWB origin workbuddy"), "codex")
        path = cx.create(self.lab.codex, projected_id, self.lab.workspace,
                         pair("Unrelated native tool history", "Tool answer"), time.time())
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "function_call"}}) + "\n")
        before = path.read_bytes()
        self.store.compatible_only = True
        result = self.store.sync(lambda: None)
        self.assertEqual(result["created"], 2)
        self.assertEqual(path.read_bytes(), before)
        native, _, deferred = self.store.selection()
        self.assertEqual(deferred["codex"], {projected_id: "unsupported_history"})
        self.assertEqual(len(native["codex"]), 2)
        self.assertTrue(any(item.messages == self.b for item in native["codex"].values()))

    def test_reopened_client_after_backup_prevents_native_write(self):
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ClientActive("Client reopened")

        with self.assertRaises(ClientActive):
            self.store.sync(guard)
        self.assertTrue(verify(self.store.last_backup))
        self.assertEqual(len(self.store.copies("codex")), 1)
        self.assertEqual(len(self.store.copies("workbuddy")), 1)

    def lifecycle_sync(self):
        self.store.compatible_only = True
        self.store.client_check = lambda _: []
        return self.store.sync(lambda: None)

    def native_action(self, sid, action):
        with Codex(self.lab.codex, self.lab.workspace) as api:
            api.call("thread/" + action, {"threadId": sid})

    def pair_a(self):
        return next(row for row in self.store.lifecycle.pairs() if row[3] == self.a_id)

    def test_archive_and_unarchive_propagate_in_both_directions(self):
        self.lifecycle_sync()
        a = self.pair_a()
        self.native_action(self.a_id, "archive")
        self.assertEqual(self.lifecycle_sync()["archived"], 1)
        self.assertEqual([r.state for r in self.store.lifecycle.replicas([a])[a[0]]], ["archived", "archived"])
        self.assertTrue(any(name.startswith("codex/archived_sessions/") for name in verify(self.store.last_backup)))
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET status='completed' WHERE id=?", (a[4],))
        self.assertEqual(self.lifecycle_sync()["unarchived"], 1)
        self.assertEqual([r.state for r in self.store.lifecycle.replicas([a])[a[0]]], ["active", "active"])
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET status='archived' WHERE id=?", (a[4],))
        self.assertEqual(self.lifecycle_sync()["archived"], 1)
        self.native_action(self.a_id, "unarchive")
        self.assertEqual(self.lifecycle_sync()["unarchived"], 1)

    def test_archive_conflict_and_initial_mismatch_keep_both_replicas(self):
        self.store.sync(lambda: None)
        self.native_action(self.a_id, "archive")
        a = self.pair_a()
        self.assertNotIn("archived", self.lifecycle_sync())
        wb = self.store.copies("workbuddy")[a[4]]
        self.assertEqual(read_messages(wb.path, "workbuddy"), self.a)
        self.native_action(self.a_id, "unarchive")
        self.lifecycle_sync()
        self.lab.publish("workbuddy", wb.sid, wb.title, self.a + pair("new", "reply"), wb)
        self.native_action(self.a_id, "archive")
        self.assertEqual(self.lifecycle_sync()["archive_conflicts"], 1)
        self.assertEqual([r.state for r in self.store.lifecycle.replicas([a])[a[0]]], ["archived", "active"])

    def test_archive_copies_source_only_followup_before_hiding_counterpart(self):
        self.lifecycle_sync()
        a = self.pair_a()
        wb = self.store.copies("workbuddy")[a[4]]
        extended = self.a + pair("new before archive", "reply")
        self.lab.publish("workbuddy", wb.sid, wb.title, extended, wb)
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET status='archived' WHERE id=?", (wb.sid,))
        result = self.lifecycle_sync()
        self.assertEqual((result["archived"], result["archive_extended"]), (1, 1))
        codex, workbuddy = self.store.lifecycle.replicas([a])[a[0]]
        self.assertEqual((codex.state, workbuddy.state), ("archived", "archived"))
        self.assertEqual(read_messages(codex.path, "codex"), extended)

    def test_reviewed_deletion_rejects_stale_snapshot_and_recovers_partial_attempt(self):
        self.lifecycle_sync()
        a = self.pair_a()
        lifecycle = self.store.lifecycle
        record, (_, wb) = next(lifecycle.delete_candidates(a[0]))
        self.native_action(self.a_id, "archive")
        with self.assertRaisesRegex(ValueError, "Preview and confirm"):
            lifecycle.delete(record["token"], lambda: None)
        self.assertTrue(wb.path.is_file())
        record, _ = next(lifecycle.delete_candidates(a[0]))
        with patch.object(lifecycle, "set_workbuddy", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                lifecycle.delete(record["token"], lambda: None)
        paths = verify(self.store.last_backup)
        self.assertTrue(any(name.startswith("codex/archived_sessions/") for name in paths))
        self.assertTrue(any(name.endswith(wb.path.name) for name in paths))
        record, _ = next(lifecycle.delete_candidates(a[0]))
        self.assertEqual(record["states"], ["missing", "active"])
        lifecycle.delete(record["token"], lambda: None)
        self.assertTrue(wb.path.is_file())
        self.assertEqual([r.state for r in lifecycle.replicas([a])[a[0]]], ["missing", "deleted"])
        self.assertEqual(self.lifecycle_sync()["deferred_groups"], 1)
        self.assertEqual(list(lifecycle.delete_candidates(a[0])), [])

    def test_native_soft_delete_alone_does_not_propagate(self):
        self.lifecycle_sync()
        a = self.pair_a()
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET deleted_at=10 WHERE id=?", (a[4],))
        self.assertEqual(self.lifecycle_sync()["deferred_groups"], 1)
        self.assertEqual([r.state for r in self.store.lifecycle.replicas([a])[a[0]]], ["active", "deleted"])

    def test_account_change_and_archive_cannot_recreate_known_session(self):
        self.store.sync(lambda: None)
        self.accounts = ("different-codex-account", OWNER)
        with self.assertRaisesRegex(ValueError, "account changed"):
            self.store.sync(lambda: None)
        self.accounts = ("test-codex-account", OWNER)
        with closing(sqlite3.connect(self.lab.wb_db)) as db, db:
            db.execute("UPDATE sessions SET status='archived' WHERE id=?", (self.b_id,))
        with self.assertRaisesRegex(ValueError, "missing, archived"):
            self.store.sync(lambda: None)
