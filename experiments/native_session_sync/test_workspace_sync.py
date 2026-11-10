"""Exercise the opt-in gate with native file layouts in a private test directory."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import errno
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from backup_lab import verify
import codex_rollout as cx
import native_sync
from native_sync import Lab, OWNER, read_messages, stable
from offline_sync import ClientActive, OfflineSync
from probe_codex import Codex
from test_native_sync import pair
from test_offline_sync import Clock
from workspace_sync import WorkspaceBatch, WorkspaceSync, report_results, run_cycles
from sync_recovery import inspect_recovery, recover


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
        self.store.schemas = tuple(replace(schema, write_fingerprints=frozenset({report.fingerprint}))
                                   for schema, report in zip(self.store.schemas, self.store.schema_reports()))

    def test_unknown_readable_schema_blocks_first_write(self):
        with closing(sqlite3.connect(self.store.wb_db)) as db, db:
            db.execute("ALTER TABLE sessions ADD COLUMN unknown_metadata TEXT NOT NULL DEFAULT ''")
        original = {path: path.read_bytes() for path in (self.store.wb_db, self.store.codex_db, self.a_path)}
        report = self.store.schema_reports()[1]
        self.assertFalse(report.invalid_columns)
        self.assertFalse(report.write_supported)
        self.assertIn("READ_ONLY_SUPPORTED UNKNOWN_SCHEMA", report.describe())
        with self.assertRaisesRegex(ValueError, "UNKNOWN_SCHEMA"):
            self.store.sync(lambda: None)
        self.assertFalse(self.store.state.exists())
        for path, expected in original.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_known_schema_allows_only_inert_optional_columns(self):
        with closing(sqlite3.connect(self.store.wb_db)) as db, db:
            db.execute("ALTER TABLE sessions ADD COLUMN new_note TEXT")
            db.execute("ALTER TABLE sessions ADD COLUMN new_index INTEGER")
        self.assertTrue(self.store.schema_reports()[1].write_supported)
        with closing(sqlite3.connect(self.store.wb_db)) as db, db:
            db.execute("CREATE TRIGGER unknown AFTER INSERT ON sessions BEGIN SELECT 1; END")
        self.assertFalse(self.store.schema_reports()[1].write_supported)

    def test_mistyped_or_missing_columns_block_even_read_validation(self):
        with closing(sqlite3.connect(self.store.wb_db)) as db, db:
            db.execute("ALTER TABLE sessions RENAME COLUMN transport TO unsupported_transport")
            db.execute("ALTER TABLE sessions RENAME COLUMN deleted_at TO old_deleted_at")
            db.execute("ALTER TABLE sessions ADD COLUMN deleted_at TEXT")
        report = self.store.schema_reports()[1]
        self.assertIn("transport", report.invalid_columns)
        self.assertIn("deleted_at", report.invalid_columns)
        with self.assertRaisesRegex(ValueError, "READ_UNSUPPORTED"):
            self.store.schemas[1].validate_read(report)

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
        other.schemas = self.store.schemas
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
        with self.assertRaisesRegex(ValueError, "RECOVERY_REQUIRED"):
            lifecycle.delete(record["token"], lambda: None)
        pending = self.store.journal.pending()
        self.assertEqual(inspect_recovery(self.store, pending["id"])["kind"], "delete")
        recover(self.store, pending["id"], lambda: None)
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

    def restart_store(self):
        old = self.store
        self.store = WorkspaceSync(old.workspace, old.codex, old.workbuddy, old.root,
                                   lambda: self.accounts, old.compatible_only, lambda _: [])
        self.store.schemas = old.schemas

    def verify_resumed_merge(self):
        self.restart_store()
        pending = self.store.journal.pending()
        self.assertIsNotNone(pending)
        op = pending["id"]
        old_plan = json.loads(pending["plan"])
        with self.assertRaisesRegex(ValueError, "RECOVERY_REQUIRED"):
            self.store.sync(lambda: None)
        report = inspect_recovery(self.store, op)
        self.assertEqual(len(report["steps"]), 4)
        self.assertEqual(recover(self.store, op, lambda: None)["verified_steps"], 4)
        self.assertIsNone(self.store.journal.pending())
        for agent in ("codex", "workbuddy"):
            self.assertCountEqual([copy.messages for copy in self.store.copies(agent).values()], [self.a, self.b])
        with closing(sqlite3.connect(self.store.state)) as db:
            record = db.execute("SELECT state,plan FROM journal_operations WHERE id=?", (op,)).fetchone()
            self.assertEqual(record[0], "RECOVERED")
            self.assertEqual(json.loads(record[1]), old_plan)
        for _ in range(10):
            self.assertEqual(self.store.sync(lambda: None)["unchanged"], 4)

    def test_actual_process_exit_after_prepared_preserves_plan(self):
        child = """
from dataclasses import replace
from pathlib import Path
import os
import sys
from workspace_sync import WorkspaceSync
workspace, codex, workbuddy, state = map(Path, sys.argv[1:5])
store = WorkspaceSync(workspace, codex, workbuddy, state, lambda: tuple(sys.argv[5:7]))
store.schemas = tuple(replace(schema, write_fingerprints=frozenset({report.fingerprint}))
                      for schema, report in zip(store.schemas, store.schema_reports()))
store.project_step = lambda *args: os._exit(77)
store.sync(lambda: None)
"""
        result = subprocess.run([sys.executable, "-B", "-c", child, str(self.store.workspace),
                                 str(self.store.codex), str(self.store.workbuddy), str(self.store.root),
                                 *self.accounts], cwd=Path(__file__).resolve().parent,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 77, (result.stdout, result.stderr))
        self.assertEqual(self.store.journal.pending()["state"], "PREPARED")
        self.verify_resumed_merge()

    def test_resume_after_codex_jsonl_before_index_commit(self):
        with patch.object(self.store, "index_codex_copy", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        self.verify_resumed_merge()

    def test_resume_workbuddy_jsonl_written_before_sqlite_commit(self):
        atomic_text = native_sync.atomic_text

        def interrupt(path, content):
            atomic_text(path, content)
            if path.is_relative_to(self.store.workbuddy):
                raise KeyboardInterrupt

        with patch.object(native_sync, "atomic_text", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        self.verify_resumed_merge()

    def test_resume_after_native_commit_before_readback(self):
        with patch.object(self.store, "verify_publication", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        self.verify_resumed_merge()

    def test_resume_after_readback_before_operation_commit(self):
        advance = self.store.journal.advance

        def interrupt(operation, state, **changes):
            if state == "COMMITTED":
                raise KeyboardInterrupt
            return advance(operation, state, **changes)

        with patch.object(self.store.journal, "advance", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        self.assertEqual(self.store.journal.pending()["completed"], 4)
        self.verify_resumed_merge()

    def test_recovery_blocks_external_history_change_and_corrupt_backup(self):
        with patch.object(self.store, "project_step", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        pending = self.store.journal.pending()
        backup = self.store.local(pending["backup"])
        before = backup.read_bytes()
        backup.write_bytes(b"invalid backup")
        with self.assertRaisesRegex(ValueError, "backup checksum"):
            recover(self.store, pending["id"], lambda: None)
        backup.write_bytes(before)
        old = self.store.copies("workbuddy")[self.b_id]
        messages = self.b + pair("external", "follow-up")
        self.lab.publish("workbuddy", old.sid, old.title, messages, old)
        history = old.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Unexpected external history"):
            recover(self.store, pending["id"], lambda: None)
        self.assertEqual(old.path.read_bytes(), history)
        self.assertEqual(self.store.journal.pending()["completed"], 0)

    def test_readback_mismatch_blocks_remaining_projection(self):
        original = self.store.verify_publication

        def mismatch(agent, sid, title, messages, current):
            wrong = messages + pair("wrong", "result")
            original(agent, sid, title, wrong, current)

        with patch.object(self.store, "verify_publication", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "publication verification failed"):
                self.store.sync(lambda: None)
        pending = self.store.journal.pending()
        self.assertEqual((pending["state"], pending["completed"]), ("RECOVERY_REQUIRED", 0))
        self.verify_resumed_merge()

    def test_disk_full_after_plan_does_not_write_native_storage(self):
        paths = (self.store.codex_db, self.store.wb_db, self.a_path)
        before = {path: path.read_bytes() for path in paths}
        with patch("workspace_sync.capture_files", side_effect=OSError(errno.ENOSPC, "fixture")):
            with self.assertRaises(OSError):
                self.store.sync(lambda: None)
        self.assertIsNone(self.store.journal.pending())
        with closing(sqlite3.connect(self.store.state)) as db:
            self.assertEqual(db.execute("SELECT state,error FROM journal_operations").fetchone(),
                             ("FAILED", "OSError"))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(self.store.sync(lambda: None)["created"], 2)

    def test_unprepared_backup_is_recoverable_without_replay(self):
        with patch.object(self.store, "_sync", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        self.assertIsNone(self.store.journal.pending())
        with closing(sqlite3.connect(self.store.state)) as db:
            row = db.execute("SELECT state,backup_hash,plan FROM journal_operations").fetchone()
            self.assertEqual(row[0], "FAILED")
            self.assertIsNotNone(row[1])
            self.assertIsNone(row[2])
        self.assertEqual(len(self.store.copies("codex")), 1)
        self.assertEqual(len(self.store.copies("workbuddy")), 1)
        self.assertEqual(self.store.sync(lambda: None)["created"], 2)

    def test_controller_lock_excludes_another_process(self):
        from native_sync import exclusive_file_lock
        lock = self.store.root.parent / "controller.lock"
        child = """
from pathlib import Path
import sys
from native_sync import exclusive_file_lock
try:
    with exclusive_file_lock(Path(sys.argv[1]), "busy"):
        pass
except RuntimeError:
    sys.exit(77)
"""
        with exclusive_file_lock(lock, "busy"):
            result = subprocess.run([sys.executable, "-B", "-c", child, str(lock)],
                                    cwd=Path(__file__).resolve().parent, timeout=15, capture_output=True)
        self.assertEqual(result.returncode, 77, (result.stdout, result.stderr))

    def test_legacy_ledger_keeps_replica_ids_when_journal_is_added(self):
        self.store.sync(lambda: None)
        with closing(sqlite3.connect(self.store.state)) as db, db:
            mappings = db.execute("SELECT * FROM copies ORDER BY agent,native").fetchall()
            db.execute("DROP TABLE journal_operations")
        self.restart_store()
        wb = self.store.copies("workbuddy")[self.b_id]
        extended = self.b + pair("legacy continuation", "reply")
        self.lab.publish("workbuddy", wb.sid, wb.title, extended, wb)
        self.assertEqual(self.store.sync(lambda: None)["extended"], 1)
        with closing(sqlite3.connect(self.store.state)) as db:
            self.assertEqual(db.execute("SELECT * FROM copies ORDER BY agent,native").fetchall(), mappings)
            self.assertEqual(db.execute("SELECT state FROM journal_operations").fetchone(), ("COMMITTED",))

    def test_database_change_between_prepare_and_apply_stops_writes(self):
        project = self.store.project_step

        def concurrent_edit(*args):
            with closing(sqlite3.connect(self.store.wb_db)) as db, db:
                db.execute("UPDATE sessions SET title='external edit' WHERE id='outside'")
            return project(*args)

        with patch.object(self.store, "project_step", side_effect=concurrent_edit):
            with self.assertRaisesRegex(ValueError, "native database changed"):
                self.store.sync(lambda: None)
        self.assertEqual(self.store.journal.pending()["completed"], 0)
        self.verify_resumed_merge()
        with closing(sqlite3.connect(self.store.wb_db)) as db:
            self.assertEqual(db.execute("SELECT title FROM sessions WHERE id='outside'").fetchone(), ("external edit",))

    def test_invalid_plan_digest_blocks_replay(self):
        with patch.object(self.store, "project_step", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.sync(lambda: None)
        pending = self.store.journal.pending()
        with closing(sqlite3.connect(self.store.state)) as db, db:
            db.execute("UPDATE journal_operations SET plan_hash='bad' WHERE id=?", (pending["id"],))
        with self.assertRaisesRegex(ValueError, "plan checksum"):
            recover(self.store, pending["id"], lambda: None)
        self.assertEqual(len(self.store.copies("codex")), 1)

    def test_archive_resume_after_native_state_change(self):
        self.lifecycle_sync()
        a = self.pair_a()
        self.native_action(self.a_id, "archive")
        advance = self.store.journal.advance

        def interrupt(operation, state, **changes):
            if state == "VERIFYING":
                raise KeyboardInterrupt
            return advance(operation, state, **changes)

        with patch.object(self.store.journal, "advance", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.lifecycle_sync()
        self.restart_store()
        operation = self.store.journal.pending()["id"]
        preview = inspect_recovery(self.store, operation)
        self.assertEqual(preview["steps"], [{"group": a[0], "state": "after"}])
        recover(self.store, operation, lambda: None)
        self.assertIsNone(self.store.journal.pending())
        self.assertEqual([item.state for item in self.store.lifecycle.replicas([a])[a[0]]], ["archived", "archived"])

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
