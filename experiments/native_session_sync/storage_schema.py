"""Separate read compatibility from recognized native write schemas."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3


@dataclass(frozen=True)
class SchemaReport:
    agent: str
    version: int
    fingerprint: str
    invalid_columns: tuple
    write_supported: bool
    error: str | None = None

    def describe(self):
        if self.error:
            return f"SCHEMA {self.agent.upper()} {self.error}"
        status = "READ_WRITE_SUPPORTED" if self.write_supported else "READ_ONLY_SUPPORTED UNKNOWN_SCHEMA"
        if self.invalid_columns:
            status = "READ_UNSUPPORTED UNKNOWN_SCHEMA"
        detail = " columns=" + ",".join(self.invalid_columns) if self.invalid_columns else ""
        return f"SCHEMA {self.agent.upper()} {status} version={self.version} sha256={self.fingerprint}{detail}"


@dataclass(frozen=True)
class StorageSchema:
    agent: str
    table: str
    text_columns: tuple
    integer_columns: tuple
    write_fingerprints: frozenset

    def detect_version(self, path):
        try:
            return self.read_schema(path)
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            status = "CORRUPT_STORAGE" if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB) else "UNAVAILABLE"
            return SchemaReport(self.agent, 0, "", (), False, status)

    def read_schema(self, path):
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
        def identify(table, fields):
            identity = json.dumps([version, table, fields, triggers, unique], separators=(",", ":"))
            return hashlib.sha256(identity.encode("utf-8")).hexdigest()

        fingerprint = identify(definition, columns)
        kinds = {name: kind.strip().upper() for _, name, kind, _, _, _, hidden in columns if not hidden}
        required = {**dict.fromkeys(self.text_columns, "TEXT"), **dict.fromkeys(self.integer_columns, "INTEGER")}
        invalid = tuple(name for name, kind in required.items() if kinds.get(name) != kind)
        if not definition or definition[0] != "table":
            invalid = (self.table, *invalid)
        supported = fingerprint in self.write_fingerprints
        if not invalid and not supported:
            sql = definition[1]
            for index in range(len(columns) - 1, max(-1, len(columns) - 9), -1):
                _, name, kind, required, default, primary, hidden = columns[index]
                if (required or default is not None or primary or hidden
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name)
                        or kind not in ("TEXT", "INTEGER", "REAL", "BLOB")):
                    break
                sql, found = re.subn(r", " + re.escape(name) + r" " + kind + r"(?=\)$)", "", sql)
                if not found:
                    break
                if identify(("table", sql), columns[:index]) in self.write_fingerprints:
                    supported = True
                    break
        return SchemaReport(self.agent, version, fingerprint, invalid, not invalid and supported)

    @staticmethod
    def validate_read(report):
        if report.invalid_columns or report.error:
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
