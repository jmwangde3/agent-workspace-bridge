"""Durable intents and checkpoints for native SQLite and transcript writes."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


class SyncJournal:
    def __init__(self, path, binding):
        self.path = path
        self.binding = binding

    @staticmethod
    def initialize(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS journal_operations (
                id TEXT PRIMARY KEY, binding TEXT NOT NULL, kind TEXT NOT NULL,
                plan TEXT, plan_hash TEXT, input_hash TEXT NOT NULL, backup TEXT NOT NULL, backup_hash TEXT,
                started TEXT NOT NULL, updated TEXT NOT NULL, state TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0, step TEXT, error TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS journal_pending ON journal_operations ((1))
            WHERE state NOT IN ('COMMITTED', 'FAILED', 'RECOVERED');
        """)

    def pending(self):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_schema WHERE name='journal_operations'").fetchone() is None:
                return None
            row = db.execute("""
                SELECT * FROM journal_operations WHERE state NOT IN ('COMMITTED', 'FAILED', 'RECOVERED')
            """).fetchone()
        if row is not None and row["binding"] != self.binding:
            raise ValueError("The unfinished operation has a different workspace or account binding")
        return dict(row) if row is not None else None

    def require_idle(self):
        pending = self.pending()
        if pending is None:
            return
        if pending["state"] in ("PLANNED", "BACKUP_VERIFIED") and pending["plan"] is None:
            self.advance(pending["id"], "FAILED", error="INTERRUPTED_BEFORE_PREPARE")
            return
        raise ValueError(f"RECOVERY_REQUIRED operation={pending['id']} type={pending['kind']} "
                         f"checkpoint={pending['state']}")

    @contextmanager
    def operation(self, kind, inputs, backup):
        operation = uuid.uuid4().hex
        now = stamp()
        with closing(sqlite3.connect(self.path)) as db, db:
            self.initialize(db)
            db.execute("""
                INSERT INTO journal_operations (id,binding,kind,input_hash,backup,started,updated,state)
                VALUES (?,?,?,?,?,?,?,'PLANNED')
            """, (operation, self.binding, kind, fingerprint(inputs), backup.name, now, now))
        try:
            yield operation
        except BaseException as error:
            self.record_failure(operation, error)
            raise

    def record_failure(self, operation, error):
        pending = self.pending()
        if pending is not None and pending["id"] == operation:
            state = "RECOVERY_REQUIRED" if pending["plan"] is not None else "FAILED"
            self.advance(operation, state, error=error.__class__.__name__)
            if state == "RECOVERY_REQUIRED":
                print("RECOVERY_REQUIRED", operation, pending["kind"], error.__class__.__name__, flush=True)

    def prepare(self, db, operation, plan):
        db.execute("""
            UPDATE journal_operations SET plan=?, plan_hash=?, state='PREPARED', updated=? WHERE id=?
        """, (json.dumps(plan, ensure_ascii=False), fingerprint(plan), stamp(), operation))

    def prepare_standalone(self, operation, plan):
        with closing(sqlite3.connect(self.path)) as db, db:
            self.prepare(db, operation, plan)

    def advance(self, operation, state, *, completed=None, step=None, error=None, backup_hash=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            result = db.execute("""
                UPDATE journal_operations
                SET state=?, updated=?, completed=coalesce(?,completed), step=coalesce(?,step), error=?,
                    backup_hash=coalesce(?,backup_hash)
                WHERE id=? AND binding=?
            """, (state, stamp(), completed, step, error, backup_hash, operation, self.binding))
            if result.rowcount != 1:
                raise ValueError("Sync checkpoint has no matching operation")

    def reviewed(self, operation):
        pending = self.pending()
        if pending is None or pending["id"] != operation:
            raise ValueError("No unfinished operation matches that ID")
        plan = json.loads(pending["plan"]) if pending["plan"] is not None else None
        unprepared = pending["state"] in ("PLANNED", "BACKUP_VERIFIED")
        if (plan is None) != unprepared:
            raise ValueError("The operation plan is inconsistent with its checkpoint")
        if plan is not None and fingerprint(plan) != pending["plan_hash"]:
            raise ValueError("The operation plan checksum does not match")
        if pending["completed"] < 0 or (plan is not None and pending["completed"] > len(plan["steps"])):
            raise ValueError("The operation checkpoint is invalid")
        return pending, plan
