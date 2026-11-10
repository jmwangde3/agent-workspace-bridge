"""Verified content backups for a workspace's canonical shared memory."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import struct
import time
import uuid
import zipfile
import zlib

import awb_links as links


MIB = 1024 * 1024
MAX_ZIP_SIZE = 512 * MIB
MAX_FILES = 10_000
MAX_FILE_SIZE = 256 * MIB
MAX_TOTAL_SIZE = 2048 * MIB
MAX_MANIFEST_SIZE = 16 * MIB
MAX_DIRECTORY_SIZE = 16 * MIB
END_RECORD = struct.Struct("<4s4H2IH")
LOCAL_HEADER = struct.Struct("<4s5H3I2H")


def digest(stream, output=None, limit=None):
    return digest_chunks(iter(lambda: stream.read(MIB), b""), output, limit)


def digest_chunks(chunks, output=None, limit=None):
    checksum = hashlib.sha256()
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if limit is not None and size > limit:
            raise ValueError("Memory backup file exceeds its byte budget")
        checksum.update(chunk)
        if output is not None:
            output.write(chunk)
    return {"size": size, "sha256": checksum.hexdigest()}


def directory_bounds(stream):
    """Bound the central directory before ZipFile allocates per-entry metadata."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    if size > MAX_ZIP_SIZE:
        raise ValueError("Memory ZIP exceeds the archive size limit")
    tail_length = min(size, END_RECORD.size + 65535)
    stream.seek(size - tail_length)
    tail = stream.read(tail_length)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or index + END_RECORD.size > len(tail):
        raise ValueError("Missing ZIP central directory")
    _, disk, cd_disk, here, count, length, offset, comment = END_RECORD.unpack_from(tail, index)
    if (disk or cd_disk or here != count or index + END_RECORD.size + comment != len(tail)
            or offset + length != size - tail_length + index):
        raise ValueError("Unsupported or inconsistent ZIP central directory")
    if count > MAX_FILES + 1 or length > MAX_DIRECTORY_SIZE:
        raise ValueError("Memory ZIP exceeds the central directory budget")
    return count, offset


def member_bytes(stream, entry, directory_start):
    """Verify actual deflate length, including bytes that ZipExtFile can truncate."""
    stream.seek(entry.header_offset)
    header = stream.read(LOCAL_HEADER.size)
    if len(header) != LOCAL_HEADER.size:
        raise ValueError("Truncated ZIP member header")
    signature, _, flags, method, _, _, _, _, _, name_size, extra_size = LOCAL_HEADER.unpack(header)
    if (signature != b"PK\x03\x04" or flags != entry.flag_bits or flags & 1 or method != entry.compress_type
            or method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
        raise ValueError("Unsupported ZIP member encoding")
    encoding = "utf-8" if flags & 0x800 else "cp437"
    if stream.read(name_size).decode(encoding) != entry.orig_filename:
        raise ValueError("ZIP local member name does not match central directory")
    stream.seek(extra_size, os.SEEK_CUR)
    remaining = entry.compress_size
    if stream.tell() + remaining > directory_start:
        raise ValueError("ZIP member overlaps the central directory")
    decoder = zlib.decompressobj(-zlib.MAX_WBITS) if method == zipfile.ZIP_DEFLATED else None
    size = crc = 0
    while remaining:
        data = stream.read(min(remaining, MIB))
        if not data:
            raise ValueError("Truncated ZIP member")
        remaining -= len(data)
        while data:
            if decoder is None:
                output, data = data, b""
            else:
                try:
                    output = decoder.decompress(data, MIB)
                except zlib.error as error:
                    raise ValueError("Invalid ZIP deflate stream") from error
                data = decoder.unconsumed_tail
                if decoder.unused_data or (decoder.eof and remaining):
                    raise ValueError("ZIP member has unexpected compressed bytes")
            size += len(output)
            if size > entry.file_size:
                raise ValueError("ZIP member decoded beyond its declared size")
            crc = zlib.crc32(output, crc)
            if output:
                yield output
    if (decoder is not None and not decoder.eof) or size != entry.file_size or crc != entry.CRC:
        raise ValueError("ZIP member size or CRC mismatch")


def check_sizes(sizes):
    total = 0
    for index, size in enumerate(sizes, 1):
        total += size
        if index > MAX_FILES:
            raise ValueError("Memory ZIP exceeds the file count limit")
        if size > MAX_FILE_SIZE:
            raise ValueError("Memory ZIP exceeds the individual file size limit")
        if total > MAX_TOTAL_SIZE:
            raise ValueError("Memory ZIP exceeds the total extracted size limit")


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
    with path.open("rb") as stream:
        count, directory_start = directory_bounds(stream)
        with zipfile.ZipFile(stream) as archive:
            yield from verify_members(workspace, archive, stream, count, directory_start)


def verify_members(workspace, archive, stream, count, directory_start):
    entries = archive.infolist()
    if len(entries) != count:
        raise ValueError("ZIP entry count does not match the central directory")
    check_sizes(entry.file_size for entry in entries if entry.filename != "manifest.json")
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
    if record.file_size > MAX_MANIFEST_SIZE:
        raise ValueError("Memory backup manifest is too large")
    content = BytesIO()
    digest_chunks(member_bytes(stream, record, directory_start), content, MAX_MANIFEST_SIZE)
    manifest = json.loads(content.getvalue().decode("utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or manifest.get("workspace") != links.normalized(workspace)):
        raise ValueError("Memory backup belongs to another workspace or format")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or {item.filename for item in entries} != {
            "manifest.json", *("memory/" + name for name in expected)}:
        raise ValueError("Memory backup manifest does not match the archive")
    paths = {name.casefold() for name in expected}
    actual = 0
    for name, identity in expected.items():
        parts = safe_parts(name)
        if any("/".join(parts[:i]).casefold() in paths for i in range(1, len(parts))):
            raise ValueError("Conflicting memory backup paths")
        entry = archive.getinfo("memory/" + name)
        if not isinstance(identity, dict) or identity.get("size") != entry.file_size:
            raise ValueError("Memory backup size mismatch: " + name)
        if entry.file_size > 64 * MIB and entry.file_size > 1000 * max(1, entry.compress_size):
            raise ValueError("Memory backup has an unsafe compression ratio: " + name)
        checked = digest_chunks(member_bytes(stream, entry, directory_start),
                                limit=min(MAX_FILE_SIZE, MAX_TOTAL_SIZE - actual))
        actual += checked["size"]
        if checked != identity:
            raise ValueError("Memory backup checksum mismatch: " + name)
    yield (lambda name: member_bytes(stream, archive.getinfo("memory/" + name), directory_start)), expected


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
        paths = []
        def source_sizes():
            for path in files(root):
                paths.append(path)
                yield path.stat().st_size
        check_sizes(source_sizes())
        total = 0
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                name = path.relative_to(root).as_posix()
                safe_parts(name)
                if links.is_link(path) or not links.within(path, root):
                    raise ValueError("Shared memory path changed during backup")
                with path.open("rb") as stream, archive.open("memory/" + name, "w", force_zip64=True) as dest:
                    identity = digest(stream, dest, min(MAX_FILE_SIZE, MAX_TOTAL_SIZE - total))
                    manifest["files"][name] = identity
                    total += identity["size"]
                if archive.fp.tell() > MAX_ZIP_SIZE:
                    raise ValueError("Memory ZIP exceeds the archive size limit")
            record = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            if len(record) > MAX_MANIFEST_SIZE:
                raise ValueError("Memory backup manifest is too large")
            archive.writestr("manifest.json", record)
        with verified_archive(workspace, temporary):
            if list(files(root)) != paths:
                raise ValueError("Shared memory changed during backup")
            for path in paths:
                with path.open("rb") as stream:
                    identity = manifest["files"][path.relative_to(root).as_posix()]
                    if digest(stream, limit=identity["size"]) != identity:
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
        elif path.stat().st_size != identity["size"]:
            yield "CONFLICT", name
        else:
            with path.open("rb") as stream:
                yield ("SAME" if digest(stream, limit=identity["size"]) == identity else "CONFLICT"), name
    for name in current:
        yield "LOCAL_ONLY", name


def recover(workspace, root, path, preview=False):
    with verified_archive(workspace, Path(path)) as (read_member, expected):
        for state, name in difference(root, expected):
            if state != "SAME":
                print("MEMORY_RESTORE_" + state, name)
        if preview:
            print("MEMORY_RESTORE_PREVIEW", len(expected))
            return None
        target = directory(workspace, ".shared/recovered-memory-" + uuid.uuid4().hex)
        staging = directory(workspace, ".shared/.recovering-" + target.name + ".partial")
        staging.mkdir(parents=True)
        try:
            total = 0
            for name, identity in expected.items():
                destination = staging.joinpath(*safe_parts(name))
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as dest:
                    checked = digest_chunks(read_member(name), dest,
                                            min(MAX_FILE_SIZE, MAX_TOTAL_SIZE - total))
                total += checked["size"]
                with destination.open("rb") as stream:
                    if checked != identity or digest(stream, limit=identity["size"]) != identity:
                        raise ValueError("Recovered memory checksum mismatch: " + name)
            for attempt in range(3):
                if target.exists() or links.is_link(target) or staging.resolve() != staging:
                    raise ValueError("Recovery destination changed")
                try:
                    staging.rename(target)
                    break
                except PermissionError:
                    if os.name != "nt" or attempt == 2:
                        raise
                    time.sleep(0.1)
        except BaseException:
            # The destination is an exclusively created, workspace-scoped directory.
            if staging.resolve() == staging and links.within(staging, workspace):
                shutil.rmtree(staging)
            raise
        print("MEMORY_RECOVERED", target)
        return target
