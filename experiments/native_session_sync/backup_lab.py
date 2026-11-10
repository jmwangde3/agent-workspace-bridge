"""Back up and verify only the lab's native session data and sync ledger."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_copy(source, destination):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, \
            closing(sqlite3.connect(destination)) as copied:
        original.backup(copied)
        if copied.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Invalid SQLite backup: " + source.name)


def capture(lab, output):
    output = lab.local(*Path(output).absolute().relative_to(lab.root).parts)
    transcripts = [*lab.codex.glob("sessions/**/*.jsonl"), *lab.codex.glob("archived_sessions/**/*.jsonl"),
                   *lab.workbuddy.glob("projects/**/*.jsonl")]
    indexes = [lab.codex / "session_index.jsonl"]
    databases = [lab.wb_db, lab.state, *lab.codex.glob("state_*.sqlite"),
                 *lab.codex.glob("thread_history_*.sqlite")]
    paths = {}
    for source in sorted(set(transcripts + indexes + databases)):
        path = lab.local(*source.relative_to(lab.root).parts)
        if path.is_file():
            paths[path.relative_to(lab.root).as_posix()] = (path, path in databases)
    with lab.sync_lock():
        return capture_files(output, paths)


def capture_files(output, paths):
    """Pack checked paths as relative ZIP members; SQLite uses a consistent snapshot."""
    with tempfile.TemporaryDirectory(prefix="backup-", dir=output.parent) as staging:
        scratch = Path(staging)
        temporary_zip = scratch / "session-backup.zip"
        manifest = {}
        with zipfile.ZipFile(temporary_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for label, (path, database) in sorted(paths.items()):
                source = path
                if database:
                    source = scratch / (str(len(manifest)) + ".sqlite")
                    sqlite_copy(path, source)
                manifest[label] = file_hash(source)
                archive.write(source, label)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        verify(temporary_zip)
        # The lab owns this output and does not overwrite previous archives.
        if output.exists():
            raise FileExistsError(output)
        temporary_zip.rename(output)
    return manifest


def verify(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        if len(names) != len(manifest) + 1 or set(names) != {"manifest.json", *manifest}:
            raise ValueError("Backup manifest does not match archive")
        for name, digest in manifest.items():
            hashed = hashlib.sha256()
            with archive.open(name) as content:
                for chunk in iter(lambda: content.read(1024 * 1024), b""):
                    hashed.update(chunk)
            if hashed.hexdigest() != digest:
                raise ValueError("Backup digest mismatch: " + name)
    return manifest
