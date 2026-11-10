"""Separate read compatibility from recognized native write schemas."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import sqlite3


@dataclass(frozen=True)
class SchemaReport:
    agent: str
    version: int
    fingerprint: str
    invalid_columns: tuple
    write_supported: bool

    def describe(self):
        read = "READ_UNSUPPORTED" if self.invalid_columns else "READ_SUPPORTED"
        write = "WRITE_SUPPORTED" if self.write_supported else "UNKNOWN_SCHEMA"
        detail = " columns=" + ",".join(self.invalid_columns) if self.invalid_columns else ""
        return f"SCHEMA {self.agent.upper()} {read} {write} version={self.version} sha256={self.fingerprint}{detail}"


@dataclass(frozen=True)
class StorageSchema:
    agent: str
    table: str
    text_columns: tuple
    integer_columns: tuple
    write_fingerprints: frozenset

    def detect_version(self, path):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            db.execute("BEGIN")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            definition = db.execute("SELECT type,sql FROM sqlite_schema WHERE name=?", (self.table,)).fetchone()
            columns = db.execute("SELECT * FROM pragma_table_xinfo(?)", (self.table,)).fetchall()
            triggers = db.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (self.table,),
            ).fetchall()
            unique = db.execute("""
                SELECT s.name,s.sql FROM sqlite_schema s
                JOIN pragma_index_list(?) i ON s.name=i.name
                WHERE i.[unique]=1 ORDER BY s.name
            """, (self.table,)).fetchall()
        identity = json.dumps([version, definition, columns, triggers, unique], separators=(",", ":"))
        fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        kinds = {name: kind.strip().upper() for _, name, kind, _, _, _, hidden in columns if not hidden}
        required = {**dict.fromkeys(self.text_columns, "TEXT"), **dict.fromkeys(self.integer_columns, "INTEGER")}
        invalid = tuple(name for name, kind in required.items() if kinds.get(name) != kind)
        if not definition or definition[0] != "table":
            invalid = (self.table, *invalid)
        return SchemaReport(self.agent, version, fingerprint, invalid,
                            not invalid and fingerprint in self.write_fingerprints)

    @staticmethod
    def validate_read(report):
        if report.invalid_columns:
            raise ValueError(report.describe())

    @staticmethod
    def validate_write(report):
        if not report.write_supported:
            raise ValueError(report.describe())


CODEX_SCHEMA = StorageSchema(
    "codex", "threads", ("id", "cwd", "creator_account_id", "source", "rollout_path"), ("archived", "archived_at"),
    frozenset({"2b127081ae8d505137fbea76e9fca95a87f8c6420dd92dcedf2700b7358e8b53"}),
)
WORKBUDDY_SCHEMA = StorageSchema(
    "workbuddy", "sessions", ("id", "title", "cwd", "user_id", "status", "transport"),
    ("deleted_at", "updated_at", "is_background_automation", "is_playground"),
    frozenset({"fa4c4842e69bba17001a892b15ccccce510a8800bdde06de62ca9bd14394e163"}),
)
