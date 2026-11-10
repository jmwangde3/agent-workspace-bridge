"""Opt-in Windows close-and-merge pilot for selected Codex/WorkBuddy workspaces."""

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid
from zipfile import BadZipFile

from backup_lab import capture_files, file_hash, verify
from native_sync import AGENTS, Copy, Lab, atomic_text, create_ledger, exclusive_file_lock, read_messages, stable
from offline_sync import ClientActive, OfflineSync, fingerprint_paths
from probe_codex import Codex
from session_lifecycle import SessionLifecycle
from storage_schema import CODEX_SCHEMA, WORKBUDDY_SCHEMA
from sync_journal import SyncJournal
from sync_recovery import backup_path, confirm_projections, inspect_recovery, projection_plan, recover


PROCESS_CHECK = r"""
$OutputEncoding=[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference='Stop'
$processes=@(Get-CimInstance Win32_Process)
$ids=@{}
foreach($p in $processes) { $ids[[int]$p.ProcessId]=$p }
foreach($p in $processes) {
    if ($p.ProcessId -eq $awbMaintenance) { continue }
    if ($p.Name -match '^codex(?:-app-server)?\.exe$') { 'Codex'; continue }
    if ($p.Name -ieq 'WorkBuddy.exe') {
        if ($p.CommandLine -match '--serve\b' -and $p.CommandLine -match '--no-session-persistence\b') { continue }
        $parent=$ids[[int]$p.ParentProcessId]
        if ($p.CommandLine -match '--control-pipe-uuid\b' -and
            (!$parent -or ($parent.CreationDate -and $p.CreationDate -and $parent.CreationDate -gt $p.CreationDate))) {
            $children=@($processes | Where-Object { $_.ParentProcessId -eq $p.ProcessId })
            if ($children.Count -eq 1 -and $children[0].Name -ieq 'WorkBuddy.exe' -and
                $children[0].CommandLine -match '--serve\b' -and
                $children[0].CommandLine -match '--no-session-persistence\b') { continue }
        }
        'WorkBuddy'; continue
    }
    if ($p.Name -ieq 'node.exe' -and $p.CommandLine -match 'WorkDaddy.+(?:daemon|watchdog)\.js') { 'WorkDaddy' }
}
"""


def active_clients(maintenance_pid=0):
    if os.name != "nt":
        raise RuntimeError("This pilot requires Windows process detection")
    command = "$awbMaintenance=" + str(int(maintenance_pid)) + "\n" + PROCESS_CHECK
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Could not inspect Windows client processes")
    active = set(result.stdout.decode("ascii", errors="replace").split())
    if not active <= {"Codex", "WorkBuddy", "WorkDaddy"}:
        raise RuntimeError("Unexpected Windows process check output")
    return sorted(active)


def read_db(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)


def scoped_path(base, path):
    if not path.is_relative_to(base) or path.resolve() != path:
        raise ValueError("A session data path leaves its expected directory")
    return path


def native_path(path):
    if os.name == "nt" and path.startswith("\\\\?\\"):
        path = "\\\\" + path[8:] if path.startswith("\\\\?\\UNC\\") else path[4:]
    return Path(path)


def same_workspace(path, workspace):
    return isinstance(path, str) and bool(path) and native_path(path).is_absolute() and native_path(path).resolve() == workspace


class WorkspaceSync(Lab):
    codex_history_mode = "paginated"

    def __init__(self, workspace, codex, workbuddy, state, accounts, compatible_only=False,
                 client_check=active_clients):
        self.workspace, self.codex, self.workbuddy, self.root = (
            Path(p).resolve() for p in (workspace, codex, workbuddy, state)
        )
        if not self.workspace.is_dir() or self.workspace == self.workspace.parent:
            raise ValueError("Choose an existing workspace directory")
        if any(self.root.is_relative_to(p) or p.is_relative_to(self.root)
               for p in (self.workspace, self.codex, self.workbuddy)):
            raise ValueError("The sync state must be outside the workspace and native stores")
        self.wb_db = self.local("workbuddy", "workbuddy.db")
        self.workbuddy_workspace = str(self.workspace)
        self.codex_db = self.local("codex", "state_5.sqlite")
        self.state = self.local("pool.sqlite")
        self.accounts = accounts
        self.codex_account, self.owner = accounts()
        self.binding = hashlib.sha256(json.dumps(
            [str(self.workspace), str(self.codex), str(self.workbuddy), self.codex_account, self.owner]
        ).encode("utf-8")).hexdigest()
        self.last_backup = None
        self.codex_indexed = 0
        self.codex_template = None
        self.compatible_only = compatible_only
        self.codex_anchors = {}
        self.client_check = client_check
        self.lifecycle = SessionLifecycle(self, read_db, same_workspace, native_path)
        self.schemas = (CODEX_SCHEMA, WORKBUDDY_SCHEMA)
        self.journal = SyncJournal(self.state, self.binding)

    read_db = staticmethod(read_db)

    def schema_reports(self):
        return [schema.detect_version(path) for schema, path in zip(self.schemas, (self.codex_db, self.wb_db))]

    def require_write_schema(self):
        for schema, report in zip(self.schemas, self.schema_reports()):
            schema.validate_write(report)

    def local(self, *parts):
        base = self.root
        if parts[0] in AGENTS:
            base = getattr(self, parts[0])
            parts = parts[1:]
        return scoped_path(base, base.joinpath(*parts))

    def check_binding(self):
        if self.accounts() != (self.codex_account, self.owner):
            raise ValueError("A client account changed during this run")
        marker = self.local("scope.json")
        if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != self.binding:
            raise ValueError("The existing ledger belongs to another workspace or account")
        if self.state.exists() and not marker.exists():
            raise ValueError("The sync ledger has no workspace binding")

    def workbuddy_cwd(self):
        return self.workbuddy_workspace

    @staticmethod
    def defer(deferred, sid, reason, error):
        if deferred is None:
            raise error
        deferred[sid] = reason

    def read_copy(self, agent, sid, path, title, deferred):
        try:
            messages = read_messages(path, agent, sid)
        except FileNotFoundError as error:
            self.defer(deferred, sid, "missing_history", error)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.defer(deferred, sid, "unsupported_history", error)
        else:
            return Copy(sid, title or messages[0][1][:80], path, messages)
        return None

    def copies(self, agent, deferred=None):
        found = {}
        hidden = set()
        if agent == "codex":
            self.codex_anchors = {}
            seen = set()
            with closing(read_db(self.codex_db)) as db:
                indexed = db.execute("SELECT id,archived,creator_account_id,cwd,source FROM threads").fetchall()
            blocked = {sid for sid, archived, owner, _, _ in indexed
                       if archived or (owner and owner != self.codex_account)}
            hidden = blocked
            expected = {sid for sid, _, _, cwd, source in indexed if sid not in blocked
                        and source in ("cli", "vscode") and same_workspace(cwd, self.workspace)}
            for candidate in (self.codex / "sessions").rglob("rollout-*.jsonl"):
                path = self.codex_file(candidate)
                with path.open(encoding="utf-8-sig") as stream:
                    try:
                        first = json.loads(stream.readline())
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                if not isinstance(first, dict) or first.get("type") != "session_meta":
                    continue
                meta = first.get("payload")
                if not isinstance(meta, dict) or not same_workspace(meta.get("cwd"), self.workspace):
                    continue
                if meta.get("source") not in ("cli", "vscode") or meta.get("parent_thread_id"):
                    continue
                try:
                    sid = str(uuid.UUID(meta["id"]))
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    self.defer(deferred, path.name, "invalid_identity", error)
                    continue
                if sid in blocked or (meta.get("creator_account_id") and
                                      meta["creator_account_id"] != self.codex_account):
                    continue
                if sid in seen:
                    self.defer(deferred, sid, "duplicate_identity", ValueError("Duplicate Codex session in workspace"))
                    found.pop(sid, None)
                    self.codex_anchors.pop(sid, None)
                    continue
                seen.add(sid)
                if meta.get("model_provider") != self.codex_model_provider:
                    self.defer(deferred, sid, "unsupported_provider", ValueError("Unsupported Codex model provider"))
                    continue
                self.codex_anchors[sid] = path
                item = self.read_copy(agent, sid, path, None, deferred)
                if item is not None:
                    found[sid] = item
            for sid in expected - found.keys() - (deferred or {}).keys():
                self.defer(deferred, sid, "missing_or_unsupported_indexed",
                           ValueError("An indexed Codex session is missing or unsupported in the selected workspace"))
        else:
            with closing(read_db(self.wb_db)) as db:
                rows = db.execute(
                    "SELECT id,title,cwd,status FROM sessions WHERE user_id=? AND deleted_at IS NULL "
                    "AND coalesce(is_background_automation,0)=0 AND coalesce(is_playground,0)=0 "
                    "AND transport='local' AND lower(status)<>'archived'", (self.owner,)
                ).fetchall()
                if deferred is not None and self.state.exists():
                    hidden = {sid for sid, in db.execute("SELECT id FROM sessions")}
            workbuddy_cwds = set()
            for sid, title, cwd, status in rows:
                if not same_workspace(cwd, self.workspace):
                    continue
                workbuddy_cwds.add(cwd)
                if status.lower() != "completed":
                    self.defer(deferred, sid, "unfinished", ValueError("The selected workspace has an unfinished WorkBuddy session"))
                    continue
                try:
                    sid = str(uuid.UUID(sid))
                except (ValueError, TypeError, AttributeError) as error:
                    self.defer(deferred, sid, "invalid_identity", error)
                    continue
                path = self.wb_path(sid)
                item = self.read_copy(agent, sid, path, title, deferred)
                if item is not None:
                    found[sid] = item
            if len(workbuddy_cwds) > 1:
                raise ValueError("WorkBuddy has multiple stored paths for the selected workspace")
            if workbuddy_cwds:
                self.workbuddy_workspace = workbuddy_cwds.pop()
        if self.state.exists():
            with closing(read_db(self.state)) as db:
                published = {sid for sid, in db.execute("SELECT native FROM published WHERE agent=?", (agent,))}
                if deferred is not None:
                    mapped = {sid for sid, in db.execute("SELECT native FROM copies WHERE agent=?", (agent,))}
                    for sid in (mapped & hidden) - found.keys():
                        deferred.setdefault(sid, "mapped_hidden")
            for sid in published - found.keys() - (deferred or {}).keys():
                self.defer(deferred, sid, "published_missing_or_hidden",
                           ValueError("A previously synced session is missing, archived, or owned by another account"))
        return found

    def selection(self):
        deferred = {agent: {} for agent in AGENTS}
        native = {agent: self.copies(agent, deferred[agent] if self.compatible_only else None) for agent in AGENTS}
        groups = set()
        if self.compatible_only and self.state.exists():
            with closing(read_db(self.state)) as db:
                mappings = db.execute("SELECT agent,native,global_id FROM copies").fetchall()
            groups = {gid for agent, sid, gid in mappings if sid in deferred[agent]}
            for agent, sid, gid in mappings:
                if gid in groups and native[agent].pop(sid, None) is not None:
                    deferred[agent][sid] = "counterpart_deferred"
        return native, groups, deferred

    @staticmethod
    def deferred_counts(deferred):
        return {agent: dict(Counter(reasons.values())) for agent, reasons in deferred.items() if reasons}

    def database_paths(self):
        paths = [self.wb_db, self.codex_db, *self.codex.glob("thread_history_*.sqlite")]
        for path in list(paths):
            paths.append(path.with_name(path.name + "-wal"))
        paths.append(self.local("codex", "session_index.jsonl"))
        return paths

    def sync_paths(self):
        paths = self.database_paths()
        paths.extend((self.codex / "sessions").rglob("rollout-*.jsonl"))
        paths.extend((self.codex / "archived_sessions").rglob("rollout-*.jsonl"))
        paths.extend(self.wb_path("sample").parent.glob("*.jsonl"))
        return paths

    def backup_paths(self, native, extra=()):
        paths = {
            "codex/state_5.sqlite": (self.codex_db, True),
            "workbuddy/workbuddy.db": (self.wb_db, True),
            "ledger/pool.sqlite": (self.state, True),
            "ledger/scope.json": (self.local("scope.json"), False),
        }
        for file in self.codex.glob("thread_history_*.sqlite"):
            paths["codex/" + file.name] = (self.codex_file(file), True)
        index = self.local("codex", "session_index.jsonl")
        if index.is_file():
            paths["codex/" + index.name] = (index, False)
        for agent, copies in native.items():
            home = getattr(self, agent)
            for item in copies.values():
                paths[agent + "/" + item.path.relative_to(home).as_posix()] = (item.path, False)
        for agent, path in extra:
            paths[agent + "/" + path.relative_to(getattr(self, agent)).as_posix()] = (path, False)
        return paths

    def backup_destination(self):
        return self.local("before-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".zip")

    def verified_backup(self, native, extra=(), backup=None):
        if backup is None:
            backup = self.backup_destination()
        capture_files(backup, self.backup_paths(native, extra))
        self.last_backup = backup

    def checkpoint_backup(self, operation, native, extra, backup):
        self.verified_backup(native, extra, backup)
        self.journal.advance(operation, "BACKUP_VERIFIED", backup_hash=file_hash(backup))

    def codex_action(self, sid, action, guard):
        guard()
        # Exclude only the native subprocess AWB owns. A newly opened client still blocks the operation.
        with Codex(self.codex, self.workspace) as api:
            self.check_binding()
            if self.client_check(api.proc.pid):
                raise ClientActive("A client opened before the Codex lifecycle operation")
            api.call("thread/" + action, {"threadId": sid})

    def choose_codex_template(self, copies):
        with closing(read_db(self.codex_db)) as db:
            db.row_factory = sqlite3.Row
            anchors = {sid: copy.path for sid, copy in copies.items()}
            if self.compatible_only:
                anchors.update(self.codex_anchors)
            for sid, path in anchors.items():
                row = db.execute("SELECT * FROM threads WHERE id=? AND archived=0", (sid,)).fetchone()
                if (row and same_workspace(row["cwd"], self.workspace)
                        and native_path(row["rollout_path"]).resolve() == path
                        and row["source"] in ("cli", "vscode")
                        and row["model_provider"] == self.codex_model_provider
                        and row["creator_account_id"] in (None, self.codex_account)
                        and (self.codex_template is None or row["source"] == "vscode")):
                    self.codex_template = row
        if self.codex_template is None:
            raise ValueError("The Codex workspace has no verified native index row to use for imports")

    def managed_codex_copy(self, sid, new):
        with closing(sqlite3.connect(self.state)) as db, db:
            row = db.execute("SELECT global_id FROM copies WHERE agent='codex' AND native=?", (sid,)).fetchone()
            already_managed = db.execute("SELECT 1 FROM managed_codex WHERE native=?", (sid,)).fetchone()
            if new or already_managed or (row and sid == stable(row[0], "codex")):
                db.execute("INSERT OR IGNORE INTO managed_codex VALUES (?)", (sid,))
                return True
        return False

    def codex_identity(self):
        identity = {key: self.codex_template[key] for key in ("source", "originator", "thread_source", "creator_user_id")}
        identity["creator_account_id"] = self.codex_account
        return identity

    def in_sync(self, native, deferred_groups=()):
        """Skip a backup only when every recorded history and index is already present."""
        copies = {(agent, sid): item.messages for agent in AGENTS for sid, item in native[agent].items()}
        with closing(read_db(self.state)) as db:
            sessions = sum(gid not in deferred_groups for gid, in db.execute("SELECT id FROM sessions"))
            rows = db.execute("SELECT c.agent,c.native,c.global_id,s.history FROM copies c "
                              "JOIN sessions s ON s.id=c.global_id").fetchall()
            published = set(db.execute("SELECT agent,native FROM published"))
            managed = {sid for sid, in db.execute("SELECT native FROM managed_codex")}
        published.difference_update((agent, sid) for agent, sid, gid, _ in rows if gid in deferred_groups)
        rows = [row for row in rows if row[2] not in deferred_groups]
        if len(rows) != len(copies) or len(rows) != sessions * len(AGENTS) or published != copies.keys():
            return False
        if any(copies.get((agent, sid)) != json.loads(history) for agent, sid, _, history in rows):
            return False
        managed.update(sid for agent, sid, gid, _ in rows if agent == "codex" and sid == stable(gid, "codex"))
        fields = {**self.codex_identity(), "cwd": self.codex_template["cwd"], "project_id": self.codex_template["project_id"]}
        with closing(read_db(self.codex_db)) as db:
            db.row_factory = sqlite3.Row
            for sid, item in native["codex"].items():
                row = db.execute("SELECT * FROM threads WHERE id=? AND archived=0", (sid,)).fetchone()
                if not row or native_path(row["rollout_path"]).resolve() != item.path:
                    return False
                if sid in managed:
                    if any(row[key] != value for key, value in fields.items()):
                        return False
                    with item.path.open(encoding="utf-8-sig") as stream:
                        meta = json.loads(stream.readline())["payload"]
                    if any(meta.get(key) != value for key, value in self.codex_identity().items()):
                        return False
        return True

    def index_codex_copy(self, sid, path, title, messages):
        template = self.codex_template
        path = self.codex_file(path)
        first, separator, tail = path.read_bytes().decode("utf-8").partition("\n")
        head = json.loads(first)
        meta = head["payload"]
        if not separator or meta["id"] != sid or meta.get("history_mode") != "paginated":
            raise ValueError("Expected an AWB paginated Codex replica")
        identity = self.codex_identity()
        rollout = str(path)
        if template["rollout_path"].startswith("\\\\?\\") and not rollout.startswith("\\\\?\\"):
            rollout = "\\\\?\\" + rollout
        with closing(sqlite3.connect(self.codex_db, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM threads WHERE id=?", (sid,)).fetchone()
            if existing and (existing["archived"] or not same_workspace(existing["cwd"], self.workspace)
                             or native_path(existing["rollout_path"]).resolve() != path
                             or existing["creator_account_id"] not in (None, self.codex_account)):
                raise ValueError("A Codex index row conflicts with the expected replica")
            if any(meta.get(key) != value for key, value in identity.items()):
                meta.update(identity)
                atomic_text(path, json.dumps(head, ensure_ascii=False, separators=(",", ":")) + "\n" + tail)
            fields = {**identity, "cwd": template["cwd"], "project_id": template["project_id"]}
            if existing:
                if all(existing[key] == value for key, value in fields.items()):
                    return False
                assignments = ",".join(key + "=:" + key for key in fields)
                db.execute("UPDATE threads SET " + assignments + " WHERE id=:id", dict(fields, id=sid))
            else:
                created = int(datetime.fromisoformat(meta["timestamp"]).timestamp() * 1000)
                updated = max(created, path.stat().st_mtime_ns // 1_000_000)
                fields.update(
                    id=sid, rollout_path=rollout, model_provider=meta["model_provider"],
                    created_at=created // 1000, updated_at=updated // 1000,
                    created_at_ms=created, updated_at_ms=updated, recency_at=updated // 1000, recency_at_ms=updated,
                    title=title, preview=messages[0][1][:250], first_user_message=messages[0][1],
                    cli_version=meta["cli_version"], history_mode="paginated", has_user_event=1,
                    sandbox_policy=template["sandbox_policy"], approval_mode=template["approval_mode"],
                )
                db.execute("INSERT INTO threads (" + ",".join(fields) + ") VALUES ("
                           + ",".join(":" + key for key in fields) + ")", fields)
        return True

    def verify_publication(self, agent, sid, title, messages, current=None):
        if agent == "codex":
            with closing(read_db(self.codex_db)) as db:
                row = db.execute(
                    "SELECT cwd,creator_account_id,archived,rollout_path,archived_at FROM threads WHERE id=?", (sid,)
                ).fetchone()
                name = db.execute("SELECT title FROM threads WHERE id=?", (sid,)).fetchone()
            item = self.lifecycle.codex_copy(sid, row, set())
        else:
            with closing(read_db(self.wb_db)) as db:
                row = db.execute(
                    "SELECT cwd,user_id,status,deleted_at,transport,updated_at FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                name = db.execute("SELECT title FROM sessions WHERE id=?", (sid,)).fetchone()
            item = self.lifecycle.wb_copy(sid, row)
        if (item.state != "active" or (current is not None and item.path != current.path)
                or (current is None and name != (title,)) or read_messages(item.path, agent, sid) != messages):
            raise ValueError(f"Native publication verification failed for {agent}/{sid}")

    def verify_integrity(self):
        for name, path, table in (("Codex", self.codex_db, "threads"), ("WorkBuddy", self.wb_db, "sessions")):
            with closing(read_db(path)) as db:
                if (db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                        or db.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None):
                    raise ValueError(f"{name} native database integrity verification failed")

    def publish(self, agent, sid, title, messages, current=None, verifying=None, before_index=None):
        if agent == "codex" and current is None:
            with closing(read_db(self.codex_db)) as db:
                if db.execute("SELECT 1 FROM threads WHERE id=?", (sid,)).fetchone():
                    raise ValueError("A planned Codex replica ID is already in the desktop index")
            if any(next((self.codex / folder).rglob("rollout-*" + sid + ".jsonl"), None) is not None
                   for folder in ("sessions", "archived_sessions")):
                raise ValueError("A planned Codex replica ID already has an unexpected transcript")
        managed = agent == "codex" and self.managed_codex_copy(sid, current is None)
        result = super().publish(agent, sid, title, messages, current)
        if managed:
            if before_index:
                before_index()
            path = current.path if current else next((self.codex / "sessions").rglob("rollout-*" + sid + ".jsonl"))
            self.codex_indexed += self.index_codex_copy(sid, path, title, messages)
        if verifying:
            verifying()
        self.verify_publication(agent, sid, title, messages, current)
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("INSERT OR IGNORE INTO published VALUES (?,?)", (agent, sid))
        return result

    def project_step(self, operation, ordinal, agent, sid, title, messages, current, guard):
        step = f"{ordinal}:{agent}/{sid}"
        self.journal.advance(operation, "APPLYING", step=step)
        guard()
        result = self.publish(agent, sid, title, messages, current,
                              verifying=lambda: self.journal.advance(operation, "VERIFYING", step=step),
                              before_index=guard)
        self.journal.advance(operation, "PREPARED", completed=ordinal + 1)
        return result

    def database_fence(self, guard):
        expected = fingerprint_paths(self.database_paths())

        def checked_guard():
            guard()
            if fingerprint_paths(self.database_paths()) != expected:
                raise ValueError("A native database changed between write steps")

        def refresh():
            nonlocal expected
            expected = fingerprint_paths(self.database_paths())

        return checked_guard, refresh

    def projection_executor(self, operation, guard, original=None):
        checked_guard, refresh = self.database_fence(guard)
        steps = None
        digests = {}
        done = set()

        def execute(ordinal, agent, sid, title, messages, current):
            nonlocal steps, digests
            if original is not None:
                if steps is None:
                    steps = self.journal.reviewed(operation)[1]["steps"]
                    digests = {(step["agent"], step["sid"]): step["before_file"] for step in steps}
                expected_file = steps[ordinal]["before_file"]
                if current is not None and file_hash(current.path) != expected_file:
                    raise ValueError(f"Native transcript changed before projection for {agent}/{sid}")
                peer_agent, peer_sid = steps[ordinal]["peer_agent"], steps[ordinal]["peer_sid"]
                peer = original[peer_agent].get(peer_sid)
                if peer is not None and (peer_agent, peer_sid) not in done:
                    if file_hash(peer.path) != digests[(peer_agent, peer_sid)]:
                        raise ValueError(f"Native source changed before projection for {agent}/{sid}")
            result = self.project_step(operation, ordinal, agent, sid, title, messages, current, checked_guard)
            done.add((agent, sid))
            refresh()
            return result

        return execute

    def sync(self, before_write=None):
        if before_write is None:
            raise ValueError("A client exit guard is required")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.sync_lock():
            self.last_backup = None

            def guard():
                before_write()
                self.check_binding()

            guard()
            self.require_write_schema()
            self.journal.require_idle()
            marker = self.local("scope.json")
            if not marker.exists():
                atomic_text(marker, json.dumps(self.binding))
            with closing(sqlite3.connect(self.state)) as db, db:
                create_ledger(db)
                db.execute("CREATE TABLE IF NOT EXISTS published (agent TEXT, native TEXT, PRIMARY KEY(agent,native))")
                db.execute("CREATE TABLE IF NOT EXISTS managed_codex (native TEXT PRIMARY KEY)")
                first_run = db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is None
            gate = OfflineSync(self, lambda: ())
            lifecycle = {}
            if self.compatible_only:
                self.lifecycle.initialize()
                lifecycle = self.lifecycle.reconcile(guard, gate.fingerprint)
            initial = gate.fingerprint()
            native, deferred_groups, deferred = self.selection()
            if not self.compatible_only and not all(native.values()):
                raise ValueError("The pilot requires a supported session from each client in this workspace")
            if not self.compatible_only and first_run and any(len(copies) != 1 for copies in native.values()):
                raise ValueError("Use a fresh pilot workspace with exactly one session per client")
            counts = self.deferred_counts(deferred)
            summary = {"deferred": counts, "deferred_groups": len(deferred_groups)} if counts else {}
            summary.update(lifecycle)
            self.codex_template = None
            if any(native.values()):
                self.choose_codex_template(native["codex"])
            if not any(native.values()) or (not first_run and self.in_sync(native, deferred_groups)):
                guard()
                if gate.fingerprint() != initial:
                    raise ClientActive("Native files changed during the check")
                return dict(sessions=len(native["codex"]), branches=0, created=0, extended=0,
                            unchanged=sum(len(copies) for copies in native.values()), **summary)
            backup = self.backup_destination()
            with self.journal.operation("merge", initial, backup) as operation:
                self.checkpoint_backup(operation, native, (), backup)
                guard()
                if gate.fingerprint() != initial:
                    raise ClientActive("Native files changed during the backup")
                self.require_write_schema()
                self.codex_indexed = 0

                def prepare(db, projections, branches):
                    self.journal.prepare(db, operation, projection_plan(projections, native, branches))

                project = self.projection_executor(operation, guard, original=native)
                result = self._sync(guard, native, deferred_groups, deferred, prepare=prepare, project=project)
                verify_snapshot = gate.fingerprint()
                confirm_projections(self, operation)
                self.verify_integrity()
                guard()
                if gate.fingerprint() != verify_snapshot:
                    raise ValueError("Native files changed during final verification")
                self.journal.advance(operation, "COMMITTED")
                result["operation_id"] = operation
            if self.compatible_only:
                self.lifecycle.observe()
            if self.codex_indexed:
                result["codex_indexed"] = self.codex_indexed
            result.update(summary)
            return result


def current_accounts(codex):
    codex_auth = json.loads((codex / "auth.json").read_text(encoding="utf-8-sig"))
    wb_file = Path(os.environ["LOCALAPPDATA"]) / "CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
    wb_auth = json.loads(wb_file.read_text(encoding="utf-8-sig"))
    accounts = codex_auth.get("tokens", {}).get("account_id"), wb_auth.get("account", {}).get("uid")
    if not all(isinstance(account, str) and account for account in accounts):
        raise ValueError("The pilot requires both clients to have a known local signed-in account")
    return accounts


@dataclass
class WorkspaceResult:
    workspace: Path
    backup: Path | None
    stats: dict | None = None
    error: str | None = None


class WorkspaceBatch:
    def __init__(self, stores):
        self.stores = stores

    def sync_paths(self):
        return set(path for store in self.stores for path in store.sync_paths())

    def sync(self, before_write):
        results = []
        for store in self.stores:
            before_write()
            try:
                stats = store.sync(before_write)
            except (ValueError, FileNotFoundError) as error:
                results.append(WorkspaceResult(store.workspace, store.last_backup, error=str(error)))
            else:
                results.append(WorkspaceResult(store.workspace, store.last_backup, stats=stats))
        return results


def report_results(store, result):
    multiple = isinstance(store, WorkspaceBatch)
    outcomes = result if multiple else [WorkspaceResult(store.workspace, store.last_backup, stats=result)]
    for outcome in outcomes:
        if multiple:
            print("WORKSPACE", outcome.workspace, flush=True)
        if outcome.backup is not None:
            print("VERIFIED_BACKUP", outcome.backup, flush=True)
        if outcome.error is not None:
            print("WORKSPACE_BLOCKED", outcome.error, flush=True)
        else:
            print("SYNC_COMPLETE", json.dumps(outcome.stats), flush=True)
    return all(outcome.error is None for outcome in outcomes)


def installed_app_ids(target):
    if os.name != "nt":
        raise RuntimeError("This launch mode requires Windows registered applications")
    command = (
        "$OutputEncoding=[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)\n"
        "$ErrorActionPreference='Stop'\n"
        "Get-StartApps | Where-Object {$_.AppID -match '^OpenAI\\.Codex_|^WorkBuddy\\.WorkBuddy$'} "
        "| ForEach-Object {$_.AppID}"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, encoding="utf-8", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Could not read installed Windows applications")
    ids = set(result.stdout.splitlines())
    matches = {
        "codex": sorted(app for app in ids if re.fullmatch(r"OpenAI\.Codex_[a-z0-9]+![A-Za-z0-9_.-]+", app)),
        "workbuddy": sorted(app for app in ids if app == "WorkBuddy.WorkBuddy"),
    }
    agents = AGENTS if target == "both" else (target,)
    if any(len(matches[agent]) != 1 for agent in agents):
        raise ValueError("A requested Windows application could not be uniquely identified")
    return [matches[agent][0] for agent in agents]


def request_windows_launch(app_ids):
    explorer = Path(os.environ["WINDIR"]) / "explorer.exe"
    for app_id in app_ids:
        subprocess.Popen([str(explorer), "shell:AppsFolder\\" + app_id])


def run_cycles(store, gate, continuous, pause=time.sleep, launch=None):
    retries = 0
    gate.request()
    print("MONITOR_READY" if continuous else "WAITING_FOR_CLIENT_EXIT", flush=True)
    while True:
        if not gate.pending and gate.active_clients():
            gate.request()
        try:
            status, result = gate.tick()
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or retries >= 3:
                raise
            retries += 1
            gate.request()
            print("SYNC_RETRY", "sqlite_busy", retries, flush=True)
            pause(2 ** retries)
            continue
        if status == "synced":
            succeeded = report_results(store, result)
            if launch is not None and succeeded:
                try:
                    gate.require_closed()
                except ClientActive:
                    gate.request()
                    pause(1)
                    continue
                launch()
                print("LAUNCH_REQUESTED", flush=True)
            elif launch is not None:
                print("LAUNCH_SKIPPED", "a workspace is blocked", flush=True)
            launch = None
            if not continuous:
                return succeeded
            retries = 0
        pause(1)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "watch", "monitor", "launch", "delete-preview", "delete",
                                           "diagnose", "verify-backup", "recovery-preview", "recover"))
    parser.add_argument("workspaces", type=Path, nargs="+")
    parser.add_argument("--open", choices=("codex", "workbuddy", "both"), dest="open_target",
                        help="open installed apps after a successful monitor or launch cycle")
    parser.add_argument("--compatible-only", action="store_true",
                        help="merge supported text sessions and defer unsupported sessions and their matched copies")
    parser.add_argument("--reviewed", help="exact pair token returned by delete-preview after review")
    parser.add_argument("--operation", help="unfinished operation ID shown by diagnose")
    parser.add_argument("--backup", help="workspace backup filename reported by AWB")
    parser.add_argument("--confirm-recovery", action="store_true")
    args = parser.parse_args()
    if args.open_target and args.action not in ("monitor", "launch"):
        parser.error("--open requires monitor or launch")
    if args.action.startswith("delete") and len(args.workspaces) != 1:
        parser.error("Deletion requires exactly one workspace")
    if bool(args.reviewed) != (args.action == "delete"):
        parser.error("delete requires a reviewed pair token")
    if args.action in ("recovery-preview", "recover"):
        if len(args.workspaces) != 1 or not args.operation:
            parser.error("Recovery requires one workspace and --operation")
    elif args.operation:
        parser.error("--operation requires recovery-preview or recover")
    if args.confirm_recovery != (args.action == "recover"):
        parser.error("recover requires --confirm-recovery")
    if bool(args.backup) != (args.action == "verify-backup") or (args.backup and len(args.workspaces) != 1):
        parser.error("verify-backup requires one workspace and --backup")
    workspaces = [path.resolve(strict=True) for path in args.workspaces]
    if len(set(workspaces)) != len(workspaces):
        parser.error("list each workspace only once")
    codex = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    workbuddy = Path.home() / ".workbuddy"
    controller = (Path(os.environ["LOCALAPPDATA"]) / "AWB/native-sync").resolve()
    stores = [
        WorkspaceSync(workspace, codex, workbuddy,
                      controller / hashlib.sha256(os.fsencode(workspace)).hexdigest()[:20],
                      lambda: current_accounts(codex), compatible_only=args.compatible_only)
        for workspace in workspaces
    ]
    if args.action == "diagnose":
        for store in stores:
            store.check_binding()
            print("WORKSPACE", store.workspace)
            for report in store.schema_reports():
                print(report.describe())
            pending = store.journal.pending()
            if pending is not None:
                details = {key: pending[key] for key in ("id", "kind", "state", "completed", "step", "error", "backup")}
                print("RECOVERY_REQUIRED", json.dumps(details))
        return
    if args.action == "recovery-preview":
        print("RECOVERY_PREVIEW", json.dumps(inspect_recovery(stores[0], args.operation)), flush=True)
        return
    if args.action == "verify-backup":
        stores[0].check_binding()
        path = backup_path(stores[0], args.backup)
        print("BACKUP_VERIFIED", path, len(verify(path)), flush=True)
        return
    if args.action == "recover":
        gate = OfflineSync(stores[0], active_clients)
        lock = scoped_path(controller, controller / "controller.lock")
        with exclusive_file_lock(lock, "Stop the AWB monitor before recovery"):
            result = recover(stores[0], args.operation, gate.require_closed)
        print("RECOVERY_COMPLETE", json.dumps(result), flush=True)
        return
    if args.action == "delete-preview":
        pending = stores[0].journal.pending()
        if pending:
            raise ValueError("RECOVERY_REQUIRED operation=" + pending["id"])
        stores[0].require_write_schema()
        print("DELETE_CANDIDATES", json.dumps([row for row, _ in stores[0].lifecycle.delete_candidates()]),
              flush=True)
        return
    if args.action == "delete":
        gate = OfflineSync(stores[0], active_clients)
        lock = scoped_path(controller, controller / "controller.lock")
        with exclusive_file_lock(lock, "Stop the AWB monitor before deleting a pair"):
            stores[0].lifecycle.delete(args.reviewed, gate.require_closed)
        print("DELETE_COMPLETE", flush=True)
        return
    if args.action == "preview":
        blocked = False
        for store in stores:
            if len(stores) > 1:
                print("WORKSPACE", store.workspace, flush=True)
            try:
                store.check_binding()
                pending = store.journal.pending()
                if pending:
                    print("RECOVERY_REQUIRED", pending["id"], pending["kind"], pending["state"], flush=True)
                reports = store.schema_reports()
                for report in reports:
                    print(report.describe(), flush=True)
                for schema, report in zip(store.schemas, reports):
                    schema.validate_read(report)
                native, groups, deferred = store.selection()
                if store.compatible_only and any(native.values()) and all(report.write_supported for report in reports):
                    store.choose_codex_template(native["codex"])
            except (ValueError, FileNotFoundError) as error:
                if len(stores) == 1:
                    raise
                print("WORKSPACE_BLOCKED", str(error), flush=True)
                blocked = True
                continue
            for agent in AGENTS:
                print(agent.upper(), len(native[agent]), flush=True)
            if counts := store.deferred_counts(deferred):
                print("DEFERRED", json.dumps({"sessions": counts, "groups": len(groups)}), flush=True)
        if blocked:
            raise SystemExit(1)
        return
    if len(stores) == 1:
        target = stores[0]
        target.check_binding()
    else:
        target = WorkspaceBatch(stores)
    open_target = args.open_target or ("both" if args.action == "launch" else None)
    app_ids = installed_app_ids(open_target) if open_target else []
    last_active = None

    def report_active():
        nonlocal last_active
        active = active_clients()
        if active != last_active:
            print("ACTIVE_CLIENTS", ",".join(active) or "none", flush=True)
            last_active = active
        return active

    gate = OfflineSync(target, report_active, settle_seconds=2)
    controller.mkdir(parents=True, exist_ok=True)
    lock = scoped_path(controller, controller / "controller.lock")
    with exclusive_file_lock(lock, "An AWB watch or monitor is already running"):
        launch = (lambda: request_windows_launch(app_ids)) if app_ids else None
        if not run_cycles(target, gate, args.action == "monitor", launch=launch):
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("SYNC_CANCELLED", flush=True)
    except (RuntimeError, OSError, ValueError, sqlite3.Error, KeyError, BadZipFile) as error:
        # Validation errors contain labels/ordinals, never native message content or credentials.
        print("SYNC_STOPPED", str(error), flush=True)
        raise SystemExit(1)
