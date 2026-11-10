"""Verified content backups for a workspace's canonical shared memory."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import uuid
import zipfile

import awb_links as links


def digest(stream, output=None):
    checksum = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        checksum.update(chunk)
        size += len(chunk)
        if output is not None:
            output.write(chunk)
    return {"size": size, "sha256": checksum.hexdigest()}


def directory(workspace, relative):
    path = Path(links.child_path(workspace, relative))
    if links.is_link(path) or not links.within(path, workspace):
        raise ValueError("Unsafe memory path: " + str(path))
    return path


def files(root):
    for path in sorted(root.iterdir()):
        if links.is_link(path):
            raise ValueError("Memory contains a link: " + str(path))
        if path.is_dir():
            yield from files(path)
        elif path.is_file():
            yield path
        else:
            raise ValueError("Unsupported memory file: " + str(path))


def safe_parts(name):
    parts = name.split("/")
    for part in parts:
        if (part in ("", ".", "..") or part.rstrip(" .") != part
                or any(ord(ch) < 32 or ch in '\\:<>"|?*' for ch in part)
                or (ntpath.isreserved(part) if hasattr(ntpath, "isreserved") else PureWindowsPath(part).is_reserved())):
            raise ValueError("Unsafe archive entry: " + name)
    return parts


@contextmanager
def verified_archive(workspace, path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        seen = set()
        for entry in entries:
            safe_parts(entry.filename)
            mode = (entry.external_attr >> 16) & 0o170000
            key = entry.filename.casefold()
            if (key in seen or entry.is_dir() or mode not in (0, stat.S_IFREG)
                    or entry.external_attr & links.REPARSE_POINT
                    or entry.orig_filename != entry.filename):
                raise ValueError("Unsupported archive entry: " + entry.filename)
            seen.add(key)
        if "manifest.json" not in archive.namelist():
            raise ValueError("Memory backup manifest is missing")
        record = archive.getinfo("manifest.json")
        if record.file_size > 16 * 1024 * 1024:
            raise ValueError("Memory backup manifest is too large")
        manifest = json.loads(archive.read(record).decode("utf-8"))
        if (not isinstance(manifest, dict) or manifest.get("version") != 1
                or manifest.get("workspace") != links.normalized(workspace)):
            raise ValueError("Memory backup belongs to another workspace or format")
        expected = manifest.get("files")
        if not isinstance(expected, dict) or {item.filename for item in entries} != {
                "manifest.json", *("memory/" + name for name in expected)}:
            raise ValueError("Memory backup manifest does not match the archive")
        paths = {name.casefold() for name in expected}
        for name, identity in expected.items():
            parts = safe_parts(name)
            if any("/".join(parts[:i]).casefold() in paths for i in range(1, len(parts))):
                raise ValueError("Conflicting memory backup paths")
            entry = archive.getinfo("memory/" + name)
            if not isinstance(identity, dict) or identity.get("size") != entry.file_size:
                raise ValueError("Memory backup size mismatch: " + name)
            with archive.open(entry) as stream:
                if digest(stream) != identity:
                    raise ValueError("Memory backup checksum mismatch: " + name)
        yield archive, expected


def backup(workspace, root):
    if not root.is_dir():
        raise ValueError("Shared memory has not been initialized")
    folder = directory(workspace, ".shared/memory-backups")
    folder.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    final = folder / ("memory-" + now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex + ".zip")
    temporary = final.with_suffix(".partial")
    manifest = {"version": 1, "workspace": links.normalized(workspace),
                "created_at": now.isoformat(), "files": {}}
    try:
        paths = list(files(root))
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                name = path.relative_to(root).as_posix()
                safe_parts(name)
                if links.is_link(path) or not links.within(path, root):
                    raise ValueError("Shared memory path changed during backup")
                with path.open("rb") as stream, archive.open("memory/" + name, "w", force_zip64=True) as dest:
                    manifest["files"][name] = digest(stream, dest)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        with verified_archive(workspace, temporary):
            if list(files(root)) != paths:
                raise ValueError("Shared memory changed during backup")
            for path in paths:
                with path.open("rb") as stream:
                    if digest(stream) != manifest["files"][path.relative_to(root).as_posix()]:
                        raise ValueError("Shared memory changed during backup: " + str(path))
        temporary.rename(final)
    finally:
        temporary.unlink(missing_ok=True)
    return final


def difference(root, expected):
    current = {path.relative_to(root).as_posix(): path for path in files(root)} if root.is_dir() else {}
    for name, identity in expected.items():
        path = current.pop(name, None)
        if path is None:
            yield "MISSING", name
        else:
            with path.open("rb") as stream:
                yield ("SAME" if digest(stream) == identity else "CONFLICT"), name
    for name in current:
        yield "LOCAL_ONLY", name


def recover(workspace, root, path, preview=False):
    with verified_archive(workspace, path) as (archive, expected):
        for state, name in difference(root, expected):
            if state != "SAME":
                print("MEMORY_RESTORE_" + state, name)
        if preview:
            print("MEMORY_RESTORE_PREVIEW", len(expected))
            return None
        target = directory(workspace, ".shared/recovered-memory-" + uuid.uuid4().hex)
        target.mkdir(parents=True)
        try:
            for name, identity in expected.items():
                destination = target.joinpath(*safe_parts(name))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("memory/" + name) as stream, destination.open("xb") as dest:
                    digest(stream, dest)
                with destination.open("rb") as stream:
                    if digest(stream) != identity:
                        raise ValueError("Recovered memory checksum mismatch: " + name)
        except BaseException:
            # The destination is an exclusively created, workspace-scoped directory.
            if target.resolve() == target and links.within(target, workspace):
                shutil.rmtree(target)
            raise
        print("MEMORY_RECOVERED", target)
        return target
