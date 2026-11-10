"""Classify and replay durable projection plans without restoring whole databases."""
from contextlib import closing
import json
import re

from backup_lab import file_hash, verify
from native_sync import AGENTS, digest, read_messages
from offline_sync import OfflineSync


def projection_plan(projections, native, branches):
    steps = []
    peers = {(group, agent): sid for group, agent, sid, _, _ in projections}
    for group, agent, sid, _, messages in projections:
        copy = native[agent].get(sid)
        peer = next(other for other in AGENTS if other != agent)
        steps.append({"group": group, "agent": agent, "sid": sid,
                      "peer_agent": peer, "peer_sid": peers.get((group, peer)),
                      "before": digest(copy.messages) if copy else None,
                      "before_file": file_hash(copy.path) if copy else None,
                      "after": digest(messages), "after_state": "active"})
    return {"steps": steps, "branches": branches}


def backup_path(store, name):
    if not re.fullmatch(r"before-[0-9-]+\.zip", name):
        raise ValueError("The recovery backup reference is invalid")
    return store.local(name)


def checked_backup(store, pending):
    backup = backup_path(store, pending["backup"])
    if file_hash(backup) != pending["backup_hash"]:
        raise ValueError("The recovery backup checksum changed")
    members = verify(backup)
    if not {"codex/state_5.sqlite", "workbuddy/workbuddy.db", "ledger/pool.sqlite"} <= members.keys():
        raise ValueError("The recovery backup lacks required members")
    return backup


def recorded_projections(store, plan):
    with closing(store.read_db(store.state)) as db:
        for step in plan["steps"]:
            row = db.execute("""
                SELECT c.global_id,s.title,s.history FROM copies c JOIN sessions s ON s.id=c.global_id
                WHERE c.agent=? AND c.native=?
            """, (step["agent"], step["sid"])).fetchone()
            if row is None or row[0] != step["group"]:
                raise ValueError("The original recovery mapping changed")
            messages = json.loads(row[2])
            if digest(messages) != step["after"]:
                raise ValueError("The original planned history checksum changed")
            yield step, row[1], messages


def classify(store, pending, plan):
    deferred = {agent: {} for agent in AGENTS}
    native = {agent: store.copies(agent, deferred[agent]) for agent in AGENTS}
    targets = []
    for ordinal, (step, title, messages) in enumerate(recorded_projections(store, plan)):
        agent, sid = step["agent"], step["sid"]
        copy = native[agent].get(sid)
        state = "before"
        if sid in deferred[agent]:
            raise ValueError(f"Recovery target {agent}/{sid} is hidden, incomplete, or incompatible")
        if copy is not None:
            current = digest(copy.messages)
            if current == step["after"]:
                state = "after"
            elif current != step["before"] or file_hash(copy.path) != step["before_file"]:
                raise ValueError(f"Unexpected external history for {agent}/{sid}")
        elif step["before"] is not None:
            raise ValueError(f"Recovery target {agent}/{sid} disappeared")
        elif agent == "workbuddy":
            path = store.wb_path(sid)
            if path.exists():
                if digest(read_messages(path, agent, sid)) != step["after"]:
                    raise ValueError(f"Unexpected unpublished transcript for {agent}/{sid}")
                state = "unindexed"
        if copy is None:
            path, table = (store.codex_db, "threads") if agent == "codex" else (store.wb_db, "sessions")
            with closing(store.read_db(path)) as db:
                if db.execute(f"SELECT 1 FROM {table} WHERE id=?", (sid,)).fetchone():
                    raise ValueError(f"Recovery target {agent}/{sid} already has an unexpected native index")
        if ordinal < pending["completed"]:
            if state != "after":
                raise ValueError(f"Completed target {agent}/{sid} changed after verification")
            store.verify_publication(agent, sid, title, messages, copy)
        targets.append((state, step, title, messages, copy))
    return targets, native


def inspect_recovery(store, operation):
    store.check_binding()
    pending, plan = store.journal.reviewed(operation)
    if plan is None:
        return {"operation": operation, "state": pending["state"], "action": "discard_unprepared"}
    store.require_write_schema()
    checked_backup(store, pending)
    if pending["kind"] != "merge":
        return store.lifecycle.inspect_recovery(pending, plan)
    targets, _ = classify(store, pending, plan)
    return {"operation": operation, "state": pending["state"], "kind": "merge",
            "steps": [{"agent": step["agent"], "sid": step["sid"], "state": state}
                      for state, step, _, _, _ in targets], "verified_steps": pending["completed"]}


def confirm_projections(store, operation):
    pending, plan = store.journal.reviewed(operation)
    if pending["completed"] != len(plan["steps"]):
        raise ValueError("A planned projection still lacks a verification checkpoint")
    classify(store, pending, plan)


def recover(store, operation, before_write):
    with store.sync_lock():
        def guard():
            before_write()
            store.check_binding()

        guard()
        pending, plan = store.journal.reviewed(operation)
        if plan is None:
            store.journal.require_idle()
            return {"operation": operation, "discarded_unprepared": True}
        store.require_write_schema()
        gate = OfflineSync(store, lambda: ())
        initial = gate.fingerprint()
        store.last_backup = checked_backup(store, pending)
        if pending["kind"] != "merge":
            return store.lifecycle.recover(pending, plan, guard, gate.fingerprint, initial)
        targets, native = classify(store, pending, plan)
        store.codex_template = None
        store.choose_codex_template(native["codex"])
        guard()
        if gate.fingerprint() != initial:
            raise ValueError("Native files changed during recovery review")
        try:
            project = store.projection_executor(operation, guard)
            for ordinal, (_, step, title, messages, copy) in enumerate(targets):
                if ordinal >= pending["completed"]:
                    project(ordinal, step["agent"], step["sid"], title, messages, copy)
            verify_snapshot = gate.fingerprint()
            confirm_projections(store, operation)
            store.verify_integrity()
            guard()
            if gate.fingerprint() != verify_snapshot:
                raise ValueError("Native files changed during final recovery verification")
            store.journal.advance(operation, "RECOVERED")
        except BaseException as error:
            store.journal.record_failure(operation, error)
            raise
        return {"operation": operation, "verified_steps": len(targets)}
