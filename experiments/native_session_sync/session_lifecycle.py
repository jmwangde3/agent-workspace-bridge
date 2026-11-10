"""Reconcile archive state and apply reviewed deletions to known native pairs."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from backup_lab import file_hash
from native_sync import AGENTS, Copy, digest, read_messages
from offline_sync import OfflineSync


VISIBLE = {"active", "archived"}


@dataclass(frozen=True)
class Replica:
    sid: str
    state: str
    path: Path | None = None
    revision: int | tuple | None = None


class SessionLifecycle:
    def __init__(self, store, read_db, same_workspace, native_path):
        self.store = store
        self.read_db = read_db
        self.same_workspace = same_workspace
        self.native_path = native_path

    def pairs(self):
        if not self.store.state.exists():
            return []
        with closing(self.read_db(self.store.state)) as db:
            return db.execute("""
                SELECT s.id, s.title, s.history, c.native, w.native
                  FROM sessions s
                  JOIN copies c ON c.global_id=s.id AND c.agent='codex'
                  JOIN copies w ON w.global_id=s.id AND w.agent='workbuddy'
                  JOIN published pc ON pc.agent=c.agent AND pc.native=c.native
                  JOIN published pw ON pw.agent=w.agent AND pw.native=w.native
                 ORDER BY s.title, s.id
            """).fetchall()

    def replicas(self, pairs):
        if not pairs:
            return {}
        s = self.store
        with closing(self.read_db(s.codex_db)) as db:
            cx = {row[0]: row[1:] for row in db.execute(
                "SELECT id, cwd, creator_account_id, archived, rollout_path, archived_at FROM threads")}
        with closing(self.read_db(s.wb_db)) as db:
            wb = {row[0]: row[1:] for row in db.execute(
                "SELECT id, cwd, user_id, status, deleted_at, transport, updated_at FROM sessions")}
        unmatched = {row[3] for row in pairs if row[3] not in cx}
        orphaned = set()
        if unmatched:
            for folder in ("sessions", "archived_sessions"):
                for path in (s.codex / folder).rglob("rollout-*.jsonl"):
                    sid = path.stem[-36:]
                    if sid in unmatched:
                        orphaned.add(sid)
        return {gid: (self.codex_copy(cid, cx.get(cid), orphaned), self.wb_copy(wid, wb.get(wid)))
                for gid, _, _, cid, wid in pairs}

    def codex_copy(self, sid, row, orphaned):
        if not row:
            return Replica(sid, "unknown" if sid in orphaned else "missing")
        cwd, owner, archived, native_file, archived_at = row
        if owner not in (None, self.store.codex_account) or not self.same_workspace(cwd, self.store.workspace):
            return Replica(sid, "unknown")
        if not isinstance(native_file, str):
            return Replica(sid, "unknown")
        path = self.native_path(native_file)
        folder = self.store.codex / ("archived_sessions" if archived else "sessions")
        if (not path.is_relative_to(folder) or path.resolve() != path or not path.is_file()
                or not path.name.endswith("-" + sid + ".jsonl")):
            return Replica(sid, "unknown")
        with path.open(encoding="utf-8-sig") as file:
            try:
                first = json.loads(file.readline())
                meta = first["payload"]
                valid = (first["type"] == "session_meta" and meta["id"] == sid
                         and self.same_workspace(meta.get("cwd"), self.store.workspace)
                         and meta.get("creator_account_id") in (None, self.store.codex_account))
            except (ValueError, KeyError, TypeError, AttributeError):
                valid = False
        if not valid:
            return Replica(sid, "unknown")
        return Replica(sid, "archived" if archived else "active", path, archived_at)

    def wb_copy(self, sid, row):
        try:
            normalized = str(uuid.UUID(sid))
        except (ValueError, TypeError, AttributeError):
            normalized = None
        if normalized != sid:
            return Replica(sid, "unknown")
        path = self.store.wb_path(sid)
        if not row:
            return Replica(sid, "unknown" if path.exists() else "missing")
        cwd, owner, status, deleted_at, transport, updated = row
        if owner != self.store.owner or transport != "local" or not self.same_workspace(cwd, self.store.workspace):
            return Replica(sid, "unknown")
        if isinstance(deleted_at, int) and deleted_at > 0:
            return Replica(sid, "deleted", path if path.is_file() else None, (updated, deleted_at))
        if (deleted_at is not None or not path.is_file() or not isinstance(status, str)
                or status.lower() not in ("completed", "archived")):
            return Replica(sid, "unknown")
        return Replica(sid, "archived" if status.lower() == "archived" else "active", path, updated)

    @staticmethod
    def paths(copies):
        return [(agent, item.path) for agent, item in zip(AGENTS, copies) if item.path]

    def initialize(self):
        with closing(sqlite3.connect(self.store.state)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS lifecycle (id TEXT PRIMARY KEY, codex TEXT, workbuddy TEXT)")

    def observe(self, changes=False):
        pairs = self.pairs()
        replicas = self.replicas(pairs)
        with closing(self.read_db(self.store.state)) as db:
            previous = {gid: (cx, wb) for gid, cx, wb in db.execute("SELECT * FROM lifecycle")}
        baseline, actions, conflicts = [], [], 0
        for pair in pairs:
            gid, _, history, _, _ = pair
            copies = replicas[gid]
            states = tuple(item.state for item in copies)
            if any(state not in VISIBLE for state in states):
                continue
            old = previous.get(gid)
            if old is None or states[0] == states[1]:
                if old != states:
                    baseline.append((gid, *states))
            elif changes and states != old:
                changed = [i for i in range(2) if states[i] != old[i]]
                histories = self.plan_history(copies, changed[0], history) if len(changed) == 1 else None
                if histories is not None:
                    source = changed[0]
                    actions.append((pair, copies, source, states[source], histories))
                else:
                    conflicts += 1
        if baseline:
            with closing(sqlite3.connect(self.store.state)) as db, db:
                db.executemany("INSERT OR REPLACE INTO lifecycle VALUES (?, ?, ?)", baseline)
        return actions, conflicts

    @staticmethod
    def plan_history(copies, source, history):
        expected = json.loads(history)
        try:
            histories = [read_messages(item.path, agent) for agent, item in zip(AGENTS, copies)]
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        source_history, target_history = histories[source], histories[1 - source]
        if source_history[:len(expected)] == expected and target_history in (expected, source_history):
            return source_history, target_history
        return None

    def set_workbuddy(self, item, state, guard):
        s = self.store
        old = "archived" if item.state == "archived" else "completed"
        guard()
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(s.wb_db, timeout=3)) as db, db:
            if state == "deleted":
                fields, values = "deleted_at=?, updated_at=?, unread=0", (now, now)
            else:
                fields = "status=?, updated_at=?"
                values = ("archived" if state == "archived" else "completed", now)
            result = db.execute(
                "UPDATE sessions SET " + fields + " WHERE id=? AND user_id=? AND transport='local' "
                "AND lower(status)=? AND deleted_at IS NULL AND updated_at=?",
                (*values, item.sid, s.owner, old, item.revision),
            )
            if result.rowcount != 1:
                raise ValueError("The WorkBuddy session changed before its status could be updated")

    def reconcile(self, guard, fingerprint):
        s = self.store
        initial = fingerprint()
        actions, conflicts = self.observe(changes=True)
        result = {"archive_conflicts": conflicts} if conflicts else {}
        if not actions:
            return result
        steps = []
        paths = []
        for pair, copies, source, state, histories in actions:
            steps.append({"group": pair[0], "before": self.snapshot(copies), "source": source, "state": state,
                          "source_history": digest(histories[0]), "target_history": digest(histories[1])})
            paths.extend(self.paths(copies))
        result.update(self.run_operation("archive", steps, paths, guard, fingerprint, initial))
        return result

    @staticmethod
    def snapshot(copies):
        return [{"sid": item.sid, "state": item.state, "hash": file_hash(item.path) if item.path else None}
                for item in copies]

    def check_step(self, kind, step, pair):
        copies = self.replicas([pair])[pair[0]]
        actual = self.snapshot(copies)
        before = step["before"]
        if [item["sid"] for item in actual] != [item["sid"] for item in before]:
            raise ValueError("The recorded native session mapping changed")
        if kind == "delete":
            expected = [dict(item) for item in before]
            if before[0]["state"] in VISIBLE:
                expected[0].update(state="missing", hash=None)
            if before[1]["state"] in VISIBLE:
                expected[1]["state"] = "deleted"
            if any(current not in (old, new) for current, old, new in zip(actual, before, expected)):
                raise ValueError("A deletion target has unexpected state or changed history")
            return copies, actual == expected

        source, state = step["source"], step["state"]
        target = 1 - source
        if actual[source] != before[source] or copies[target].state not in (before[target]["state"], state):
            raise ValueError("An archive source or target has unexpected state")
        changed_history = state == "archived" and step["source_history"] != step["target_history"]
        desired = step["source_history"] if changed_history else step["target_history"]
        target_hash = digest(read_messages(copies[target].path, AGENTS[target], copies[target].sid))
        if target_hash not in (step["target_history"], desired):
            raise ValueError("An archive target has unexpected history")
        if target_hash == step["target_history"] and actual[target]["hash"] != before[target]["hash"]:
            raise ValueError("An archive target transcript changed outside AWB")
        after = copies[target].state == state and target_hash == desired
        if copies[target].state == state and not after:
            raise ValueError("The archive target changed state without the planned history")
        return copies, after

    def inspected_steps(self, pending, plan):
        pairs = {pair[0]: pair for pair in self.pairs()}
        for ordinal, step in enumerate(plan["steps"]):
            pair = pairs.get(step["group"])
            if pair is None:
                raise ValueError("The recorded lifecycle pair is missing")
            copies, after = self.check_step(pending["kind"], step, pair)
            if ordinal < pending["completed"] and not after:
                raise ValueError("A verified lifecycle pair changed after completion")
            yield step, pair, copies, after

    def inspect_recovery(self, pending, plan):
        return {"operation": pending["id"], "kind": pending["kind"], "state": pending["state"],
                "steps": [{"group": pair[0], "state": "after" if after else "replay"}
                          for _, pair, _, after in self.inspected_steps(pending, plan)]}

    def recover(self, pending, plan, guard, fingerprint, initial):
        targets = list(self.inspected_steps(pending, plan))
        guard()
        if fingerprint() != initial:
            raise ValueError("Native files changed during lifecycle recovery review")
        try:
            return self.apply_plan(pending, targets, guard, "RECOVERED")
        except BaseException as error:
            self.store.journal.record_failure(pending["id"], error)
            raise

    def run_operation(self, kind, steps, paths, guard, fingerprint, initial):
        s = self.store
        guard()
        backup = s.backup_destination()
        with s.journal.operation(kind, initial, backup) as operation:
            s.checkpoint_backup(operation, {}, paths, backup)
            guard()
            if fingerprint() != initial:
                raise ValueError("Native session files changed during the lifecycle backup")
            s.require_write_schema()
            plan = {"steps": steps}
            pending = {"id": operation, "kind": kind, "completed": 0}
            targets = list(self.inspected_steps(pending, plan))
            s.journal.prepare_standalone(operation, plan)
            print("VERIFIED_BACKUP", s.last_backup, flush=True)
            return self.apply_plan(pending, targets, guard, "COMMITTED")

    def apply_plan(self, pending, targets, guard, final_state):
        s = self.store
        check_db, refresh_db = s.database_fence(guard)
        operation, kind = pending["id"], pending["kind"]
        result = {"operation_id": operation}
        for ordinal, (step, pair, _, _) in enumerate(targets):
            if ordinal < pending["completed"]:
                continue
            check_db()
            copies, after = self.check_step(kind, step, pair)
            s.journal.advance(operation, "APPLYING", step=f"{ordinal}:{kind}/{pair[0]}")
            if kind == "delete":
                if copies[0].state in VISIBLE:
                    s.codex_action(copies[0].sid, "delete", check_db)
                    refresh_db()
                check_db()
                copies, _ = self.check_step(kind, step, pair)
                if copies[1].state in VISIBLE:
                    self.set_workbuddy(copies[1], "deleted", check_db)
                    refresh_db()
            else:
                extended = self.apply_archive(step, pair, copies, after, check_db, refresh_db)
                if extended:
                    result["archive_extended"] = result.get("archive_extended", 0) + 1
                key = "archived" if step["state"] == "archived" else "unarchived"
                result[key] = result.get(key, 0) + 1
            s.journal.advance(operation, "VERIFYING")
            check_db()
            _, after = self.check_step(kind, step, pair)
            if not after:
                raise ValueError("Lifecycle read-back did not match the original plan")
            if kind == "archive":
                source, state = step["source"], step["state"]
                with closing(sqlite3.connect(s.state)) as db, db:
                    db.execute("UPDATE lifecycle SET codex=?, workbuddy=? WHERE id=?", (state, state, pair[0]))
                    if state == "archived":
                        history = read_messages(copies[source].path, AGENTS[source], copies[source].sid)
                        db.execute("UPDATE sessions SET history=? WHERE id=?", (json.dumps(history), pair[0]))
            s.journal.advance(operation, "PREPARED", completed=ordinal + 1)
        for step, pair, _, _ in targets:
            if not self.check_step(kind, step, pair)[1]:
                raise ValueError("A lifecycle target changed after verification")
        s.verify_integrity()
        check_db()
        s.journal.advance(operation, final_state)
        return result

    def apply_archive(self, step, pair, copies, after, guard, refresh_db):
        if after:
            return False
        s = self.store
        source, state = step["source"], step["state"]
        target = 1 - source
        item = copies[target]
        extended = False
        if state == "archived":
            messages = read_messages(copies[source].path, AGENTS[source], copies[source].sid)
            old = read_messages(item.path, AGENTS[target], item.sid)
            if old != messages:
                current = Copy(item.sid, pair[1], item.path, old)
                if target == 0:
                    s.codex_template = None
                    s.choose_codex_template({item.sid: current})
                guard()
                s.publish(AGENTS[target], item.sid, pair[1], messages, current, before_index=guard)
                refresh_db()
                item = self.replicas([pair])[pair[0]][target]
                extended = True
        if target == 0:
            s.codex_action(item.sid, "archive" if state == "archived" else "unarchive", guard)
        else:
            self.set_workbuddy(item, state, guard)
        refresh_db()
        return extended

    @staticmethod
    def review_key(binding, gid, copies):
        fields = [binding, gid]
        for item in copies:
            checksum = None
            if item.path:
                digest = hashlib.sha256()
                with item.path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                checksum = digest.hexdigest()
            fields.append((item.sid, item.state, str(item.path), item.revision, checksum))
        return gid + ":" + hashlib.sha256(json.dumps(fields).encode("utf-8")).hexdigest()

    def delete_candidates(self, gid=None):
        self.store.check_binding()
        pairs = [pair for pair in self.pairs() if gid is None or pair[0] == gid]
        replicas = self.replicas(pairs)
        for group, title, _, _, _ in pairs:
            copies = replicas[group]
            states = [item.state for item in copies]
            if "unknown" in states or VISIBLE.isdisjoint(states):
                continue
            yield ({"title": title, "states": states, "token": self.review_key(self.store.binding, group, copies)},
                   copies)

    def delete(self, token, guard):
        s = self.store
        with s.sync_lock():
            guard()
            s.require_write_schema()
            s.journal.require_idle()

            def recheck():
                guard()
                result = next(self.delete_candidates(token.split(":", 1)[0]), None)
                if result is None or result[0]["token"] != token:
                    raise ValueError("The reviewed pair changed. Preview and confirm it again")
                return result[1]

            cx, wb = recheck()
            def reviewed_guard():
                guard()
                s.check_binding()
            fingerprint = OfflineSync(s, lambda: ()).fingerprint
            initial = fingerprint()
            cx, wb = recheck()
            steps = [{"group": token.split(":", 1)[0], "before": self.snapshot((cx, wb))}]
            return self.run_operation("delete", steps, self.paths((cx, wb)), reviewed_guard, fingerprint, initial)
