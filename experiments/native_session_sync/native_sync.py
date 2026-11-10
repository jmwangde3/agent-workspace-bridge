"""Offline sync lab for synthetic text-only Codex and WorkBuddy stores.

There are no production paths, network calls, or model calls here.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

import codex_rollout as cx

AGENTS = ("codex", "workbuddy")
OWNER = "awb-fixture-owner"
MARKER = "AWB-native-sync-fixture-1"
WORKBUDDY_REMINDER = re.compile(
    r'(?:<system-reminder data-role="(?:user-context|additional-data)">'
    r'[\s\S]*?</system-reminder>\r?\n)'
)


def stable(key, value):
    return str(uuid.uuid5(uuid.UUID(key), value))


def digest(turns):
    return hashlib.sha256(json.dumps(turns, ensure_ascii=False).encode("utf-8")).hexdigest()


def prefix(shorter, longer):
    return longer[:len(shorter)] == shorter


def check_turns(turns):
    if not turns or len(turns) % 2 or any(
            role != ("user" if i % 2 == 0 else "assistant") or not isinstance(text, str)
            for i, (role, text) in enumerate(turns)):
        raise ValueError("Only complete alternating text turns are supported in the lab")


def create_ledger(db):
    db.executescript("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, history TEXT NOT NULL, parent TEXT
    );
    CREATE TABLE IF NOT EXISTS copies (
        agent TEXT NOT NULL, native TEXT NOT NULL, global_id TEXT NOT NULL,
        PRIMARY KEY(agent, native), UNIQUE(agent, global_id)
    );""")


@contextmanager
def exclusive_file_lock(path, busy_message):
    with path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError(busy_message) from error
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(busy_message) from error
        yield


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".awb-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def workbuddy_log_metadata(event):
    kind = event.get("type")
    if kind == "ai-title":
        return True
    if kind == "session-meta":
        meta = event.get("meta")
        return isinstance(meta, dict) and meta.keys() <= {"codebuddy.ai/hostKind", "codebuddy.ai/sessionTitle"}
    if kind == "file-history-snapshot":
        snapshot = event.get("snapshot")
        return isinstance(snapshot, dict) and snapshot.get("trackedFileBackups") == {}
    if kind == "reasoning":
        raw = event.get("rawContent")
        return event.get("content") == [] and isinstance(raw, list) and all(
            isinstance(block, dict) and block.get("type") == "reasoning_text"
            and isinstance(block.get("text"), str) for block in raw
        )
    return False


def workbuddy_user_text(event, text, number):
    provider = event.get("providerData")
    if not isinstance(provider, dict) or provider.get("startsNewUserRequest") is not True:
        return text
    if any(tag in text for tag in ("<attachment_sandbox_paths>", "<image_local_path>", "<long_text_quote>")):
        raise ValueError("Unsupported WorkBuddy text attachment at line %d" % number)
    if text.startswith("<system-reminder"):
        end = 0
        while match := WORKBUDDY_REMINDER.match(text, end):
            end = match.end()
        query = text[end:]
        if not end or not query.startswith("<user_query>") or not query.endswith("</user_query>"):
            raise ValueError("Unsupported WorkBuddy prompt context at line %d" % number)
        text = query.removeprefix("<user_query>").removesuffix("</user_query>")
    return text


def read_messages(path, agent):
    messages = []
    with path.open(encoding="utf-8-sig") as handle:
        if agent == "codex":
            meta = json.loads(handle.readline())
            mode = meta.get("payload", {}).get("history_mode", "legacy")
            if mode == "paginated":
                messages = cx.paginated_messages(meta, handle)
                check_turns(messages)
                return messages
            if mode != "legacy":
                raise ValueError("Unsupported Codex history mode")
            handle.seek(0)
        for number, line in enumerate(handle, 1):
            event = json.loads(line)
            kind = event.get("type")
            if agent == "codex":
                if kind not in ("session_meta", "event_msg", "turn_context", "response_item"):
                    raise ValueError("Unsupported Codex rollout record at line %d" % number)
                if kind != "response_item":
                    continue
                event = event.get("payload", {})
            elif workbuddy_log_metadata(event):
                continue
            role = event.get("role")
            parts = event.get("content")
            expected = "input_text" if role == "user" else "output_text"
            if (event.get("type") != "message" or role not in ("user", "assistant")
                    or not isinstance(parts, list) or len(parts) != 1
                    or not isinstance(parts[0], dict) or parts[0].get("type") != expected
                    or not isinstance(parts[0].get("text"), str)):
                raise ValueError("Unsupported model history at line %d" % number)
            text = parts[0]["text"]
            if agent == "workbuddy" and role == "user":
                text = workbuddy_user_text(event, text, number)
            messages.append([role, text])
    check_turns(messages)
    return messages


@dataclass(frozen=True)
class Copy:
    sid: str
    title: str
    path: Path
    messages: list


class Lab:
    owner = OWNER
    codex_history_mode = "legacy"
    codex_model_provider = "openai"
    codex_account = None

    def __init__(self, root, create=False):
        self.root = Path(root).resolve()
        if create:
            self.root.mkdir(parents=True, exist_ok=False)
            (self.root / "workspace").mkdir()
            (self.root / "codex").mkdir()
            (self.root / "workbuddy").mkdir()
            (self.root / "LAB").write_text(MARKER, encoding="utf-8")
        if (self.root / "LAB").read_text(encoding="utf-8") != MARKER:
            raise ValueError("This tool requires a dedicated synthetic lab")
        self.workspace = self.local("workspace")
        self.codex = self.local("codex")
        self.workbuddy = self.local("workbuddy")
        self.wb_db = self.local("workbuddy", "workbuddy.db")
        self.state = self.local("pool.sqlite")
        if create:
            with closing(sqlite3.connect(self.wb_db)) as db, db:
                db.execute("""CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, cwd TEXT NOT NULL, user_id TEXT NOT NULL, title TEXT,
                    status TEXT NOT NULL DEFAULT 'Pending', created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL, last_activity_at INTEGER,
                    is_playground INTEGER NOT NULL DEFAULT 0, mode TEXT,
                    deleted_at INTEGER, is_background_automation INTEGER,
                    transport TEXT NOT NULL DEFAULT 'local', unread INTEGER NOT NULL DEFAULT 0
                )""")
            with closing(sqlite3.connect(self.state)) as db, db:
                create_ledger(db)

    def codex_file(self, path):
        return self.local("codex", *path.relative_to(self.codex).parts)

    def sync_paths(self):
        paths = [self.wb_db, self.wb_db.with_name(self.wb_db.name + "-wal")]
        for root in (self.codex / "sessions", self.workbuddy / "projects"):
            paths.extend(root.rglob("*.jsonl"))
        return [self.local(*path.relative_to(self.root).parts) for path in paths]

    def local(self, *parts):
        path = self.root.joinpath(*parts)
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise ValueError("Lab paths cannot leave the fixture directory")
        return path

    def wb_path(self, sid):
        drive, *parts = self.workspace.parts
        slug = "-".join([drive.rstrip(":\\/").lower(), *parts]).lstrip("-")
        return self.local("workbuddy", "projects", slug, sid + ".jsonl")

    def workbuddy_cwd(self):
        return str(self.workspace)

    def copies(self, agent):
        found = {}
        if agent == "codex":
            for candidate in (self.codex / "sessions").rglob("rollout-*.jsonl"):
                path = self.codex_file(candidate)
                with path.open(encoding="utf-8") as f:
                    header = json.loads(f.readline())
                meta = header.get("payload", {})
                if header.get("type") != "session_meta" or meta.get("cwd") != str(self.workspace):
                    raise ValueError("Unexpected Codex fixture header")
                sid = str(uuid.UUID(meta["id"]))
                if sid in found:
                    raise ValueError("Duplicate Codex native ID")
                messages = read_messages(path, agent)
                found[sid] = Copy(sid, messages[0][1][:80], path, messages)
        else:
            with closing(sqlite3.connect(self.wb_db.as_uri() + "?mode=ro", uri=True)) as db:
                rows = db.execute("SELECT id, title, cwd, user_id FROM sessions").fetchall()
            for sid, title, cwd, owner in rows:
                if cwd != str(self.workspace) or owner != self.owner:
                    raise ValueError("Unexpected WorkBuddy fixture owner or workspace")
                sid = str(uuid.UUID(sid))
                path = self.wb_path(sid)
                found[sid] = Copy(sid, title, path, read_messages(path, agent))
        return found

    def publish(self, agent, sid, title, messages, current=None):
        check_turns(messages)
        if current and read_messages(current.path, agent) != current.messages:
            raise ValueError("Native history changed after it was read")
        if current and current.messages == messages:
            return "unchanged"
        old = current.messages if current else []
        if not prefix(old, messages):
            raise ValueError("Refusing to overwrite divergent native history")
        now = time.time()
        if agent == "codex":
            if current:
                content = current.path.read_bytes().decode("utf-8")
                appended = cx.appended_records(content, sid, len(old) // 2, messages[len(old):], now)
                atomic_text(current.path, content + appended)
            else:
                destination = cx.path_for(self.codex, sid, now)
                self.codex_file(destination)
                cx.create(self.codex, sid, self.workspace, messages, now, self.codex_history_mode,
                          self.codex_model_provider, self.codex_account)
        else:
            path = self.wb_path(sid)
            cwd = self.workbuddy_cwd()
            stamp = int(now * 1000)
            before = path.read_bytes().decode("utf-8") if path.exists() else ""
            if before and not before.endswith("\n"):
                raise ValueError("Cannot append to an incomplete WorkBuddy transcript")
            if before and not current:
                # An interruption may have left the log before SQLite committed.
                if read_messages(path, agent) != messages:
                    raise ValueError("Unindexed WorkBuddy log conflicts with recovery")
                old = messages
            rows = [] if before else [{"type": "ai-title", "sessionId": sid, "cwd": cwd,
                                       "aiTitle": title, "timestamp": stamp}]
            for pos, (role, value) in enumerate(messages[len(old):], start=len(old)):
                rows.append({"type": "message", "role": role, "sessionId": sid,
                             "cwd": cwd, "status": "completed", "timestamp": stamp + pos,
                             "id": stable(sid, "message-%d" % pos),
                             "content": [{"type": "input_text" if role == "user" else "output_text",
                                          "text": value}]})
            with closing(sqlite3.connect(self.wb_db)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if current:
                    updated = db.execute("UPDATE sessions SET updated_at=?, last_activity_at=? "
                                         "WHERE id=? AND user_id=? AND deleted_at IS NULL",
                                         (stamp, stamp, sid, self.owner))
                    if updated.rowcount != 1:
                        raise ValueError("WorkBuddy session owner or visibility changed")
                else:
                    db.execute("""INSERT INTO sessions (id,cwd,user_id,title,status,created_at,updated_at,
                                  last_activity_at,mode) VALUES (?,?,?,?,?,?,?,?,?)""",
                               (sid, cwd, self.owner, title, "completed", stamp, stamp, stamp, "craft"))
                if rows:
                    atomic_text(path, before + cx.encode(rows))
        return "extended" if current else "created"

    def seed(self, agent, title, messages):
        sid = str(uuid.uuid4())
        self.publish(agent, sid, title, messages)
        return sid

    def sync(self, before_write=None):
        with self.sync_lock():
            return self._sync(before_write)

    def sync_lock(self):
        return exclusive_file_lock(self.local("sync.lock"), "An offline sync is already running")

    def _sync(self, before_write=None, native=None, deferred_groups=(), reserved=None):
        if before_write:
            before_write()
        if native is None:
            native = {agent: self.copies(agent) for agent in AGENTS}
        if reserved is None:
            reserved = {agent: () for agent in AGENTS}
        branches = 0
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for agent, copies in native.items():
                for sid, item in copies.items():
                    mapped = db.execute("SELECT global_id FROM copies WHERE agent=? AND native=?",
                                        (agent, sid)).fetchone()
                    gid = mapped[0] if mapped else stable(sid, "AWB origin " + agent)
                    if gid in deferred_groups:
                        continue
                    row = db.execute("SELECT history FROM sessions WHERE id=?", (gid,)).fetchone()
                    current = json.loads(row[0]) if row else []
                    parent = None
                    if not prefix(current, item.messages) and not prefix(item.messages, current):
                        parent, gid = gid, stable(gid, digest(item.messages))
                        branches += 1
                    if parent or len(item.messages) > len(current):
                        db.execute("""INSERT INTO sessions VALUES (?,?,?,?)
                                      ON CONFLICT(id) DO UPDATE SET history=excluded.history""",
                                   (gid, item.title, json.dumps(item.messages, ensure_ascii=False), parent))
                    db.execute("""INSERT INTO copies VALUES (?,?,?)
                                  ON CONFLICT(agent,native) DO UPDATE SET global_id=excluded.global_id""",
                               (agent, sid, gid))
            projections = []
            for gid, title, history, _ in db.execute("SELECT id,title,history,parent FROM sessions ORDER BY id"):
                if gid in deferred_groups:
                    continue
                messages = json.loads(history)
                for agent in AGENTS:
                    row = db.execute("SELECT native FROM copies WHERE agent=? AND global_id=?",
                                     (agent, gid)).fetchone()
                    sid = row[0] if row else stable(gid, agent)
                    if not row:
                        attempt = 0
                        while sid in reserved[agent] or db.execute(
                                "SELECT 1 FROM copies WHERE agent=? AND native=?", (agent, sid)).fetchone():
                            attempt += 1
                            sid = stable(gid, "%s replica %d" % (agent, attempt))
                        db.execute("INSERT INTO copies VALUES (?,?,?)", (agent, sid, gid))
                    elif sid in reserved[agent]:
                        raise ValueError("A planned replica collides with a deferred native session")
                    projections.append((agent, sid, title, messages))
        # Intent is committed first. Deterministic replica IDs allow an interrupted projection to retry.
        stats = {"sessions": len(projections) // len(AGENTS), "branches": branches,
                 "created": 0, "extended": 0, "unchanged": 0}
        for agent, sid, title, messages in projections:
            if before_write:
                before_write()
            result = self.publish(agent, sid, title, messages, native[agent].get(sid))
            stats[result] += 1
        return stats
