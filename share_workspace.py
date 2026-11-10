"""Manage the shared workspace memory directory and its WorkBuddy link."""
import argparse
import datetime
import filecmp
import json
import os
import shutil
import sys
import uuid
from zipfile import BadZipFile

import awb_config as C
import awb_links as L
import awb_memory_backup as backups

MEMORY = ".shared/memory"
ALIAS = ".workbuddy/memory"


def is_reparse(path):
    return L.is_link(path)


def mk_junction(link, target):
    L.ensure_link(target, link)


def memory_skeleton(ws, memory):
    index = os.path.join(memory, "MEMORY.md")
    if not os.path.exists(index):
        with open(index, "w", encoding="utf-8") as handle:
            handle.write("# %s shared memory\n\n## Current\n\n## References\n" % os.path.basename(ws))
    day = os.path.join(memory, datetime.date.today().isoformat() + ".md")
    if not os.path.exists(day):
        with open(day, "w", encoding="utf-8") as handle:
            handle.write("# %s\n\n" % datetime.date.today().isoformat())


def merge_memory(source, target, label="imported"):
    """Copy and verify all regular content; retain both sides of conflicts."""
    os.makedirs(target, exist_ok=True)
    for name in os.listdir(source):
        src, dst = os.path.join(source, name), os.path.join(target, name)
        if L.is_link(src) or L.is_link(dst):
            raise ValueError("Memory contains a link; preserved for manual review: " + src)
        if os.path.isdir(src):
            if os.path.exists(dst) and not os.path.isdir(dst):
                dst += "." + label + "-directory"
                while L.is_link(dst) or os.path.isfile(dst):
                    dst += "-" + uuid.uuid4().hex[:8]
                print("MEMORY_CONFLICT:", src, "->", dst)
            merge_memory(src, dst, label)
        elif os.path.isfile(src):
            if os.path.exists(dst):
                if os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False):
                    continue
                stem = dst + "." + label + "-"
                matches = (os.path.join(target, entry) for entry in os.listdir(target)
                           if entry.startswith(os.path.basename(stem)))
                if any(not L.is_link(item) and os.path.isfile(item) and filecmp.cmp(src, item, shallow=False)
                       for item in matches):
                    continue
                dst = stem + uuid.uuid4().hex[:8]
                print("MEMORY_CONFLICT:", src, "->", dst)
            temp = os.path.join(target, ".memory-copy-" + uuid.uuid4().hex)
            try:
                with open(temp, "xb"):
                    pass
                shutil.copy2(src, temp)
                if not filecmp.cmp(src, temp, shallow=False):
                    raise OSError("Memory copy verification failed: " + src)
                if os.name == "nt":
                    os.rename(temp, dst)  # Windows rename refuses to overwrite a competing file.
                else:
                    os.link(temp, dst)
            finally:
                if os.path.lexists(temp):
                    os.unlink(temp)


def convert_dir(ws, rel, target):
    p = L.child_path(ws, rel)
    if L.is_link(p):
        changed = L.ensure_link(target, p)
        print(rel, "repaired" if changed else "unchanged")
        return
    if os.path.exists(p):
        if not os.path.isdir(p):
            raise ValueError("Real file blocks agent directory: " + p)
        backup = p + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        os.rename(p, backup)
        try:
            mk_junction(p, target)
        except Exception:
            if not os.path.lexists(p):
                os.rename(backup, p)
            raise
        print(rel, "backed up ->", backup)
    else:
        mk_junction(p, target)


def why(ws, rel):
    """溯源：某个 agent 路径的内容来自哪个 canonical。"""
    p = os.path.join(ws, *rel.split("/"))
    if not os.path.lexists(p):
        print(rel, "-> (missing)")
        return
    if is_reparse(p):
        try:
            t = os.readlink(p)
        except OSError:
            t = "(junction)"
        print("%s\n  JUNCTION -> %s\n  canonical -> %s" % (rel, t, os.path.realpath(p)))
    else:
        print(rel, "-> real directory (not shared)")


def build(ws):
    memory = L.child_path(ws, MEMORY)
    if not L.within(memory, ws):
        raise ValueError("Canonical memory escapes the workspace")
    alias = L.child_path(ws, ALIAS)
    os.makedirs(memory, exist_ok=True)
    if os.path.isdir(alias) and not L.is_link(alias):
        merge_memory(alias, memory)
    memory_skeleton(ws, memory)
    convert_dir(ws, ALIAS, memory)


def status(ws):
    print("WORKSPACE", ws)
    for relative in (MEMORY, ALIAS):
        why(ws, relative)


def check(ws):
    memory = L.child_path(ws, MEMORY)
    alias = L.child_path(ws, ALIAS)
    ok = (L.within(memory, ws) and os.path.isfile(os.path.join(memory, "MEMORY.md"))
          and L.is_link(alias) and L.same_target(alias, memory))
    print("MEMORY_CHECK", "OK" if ok else "FAILED", ws)
    return 0 if ok else 1


def probe(ws):
    if check(ws):
        return 1
    name = "_share_probe_" + uuid.uuid4().hex + ".txt"
    path = os.path.join(ws, MEMORY, name)
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(name)
    try:
        hit = L.same_target(os.path.join(ws, ALIAS, name), path)
        print("MEMORY_PROBE", "OK" if hit else "FAILED")
        return 0 if hit else 1
    finally:
        os.remove(path)


def snapshot(ws):
    folder = backups.directory(ws, ".shared")
    memory = backups.directory(ws, MEMORY)
    alias = L.child_path(ws, ALIAS)
    folder.mkdir(exist_ok=True)
    now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    files = sorted(os.listdir(memory)) if os.path.isdir(memory) else []
    path = os.path.join(folder, "snapshot-" + now + ".json")
    with open(path, "x", encoding="utf-8") as handle:
        json.dump({"workspace": os.path.basename(ws), "workspace_path": L.normalized(ws),
                   "kind": "memory-link-state", "canonical": str(memory),
                   "alias_target": os.readlink(alias) if L.is_link(alias) else None,
                   "link_ok": L.is_link(alias) and L.same_target(alias, memory),
                   "memory_files": files}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("SNAPSHOT", path)


def restore(ws, path):
    with open(path, encoding="utf-8") as handle:
        old = json.load(handle)
    if not isinstance(old, dict) or old.get("workspace") != os.path.basename(ws):
        raise ValueError("The snapshot belongs to another workspace")
    if "kind" in old and (old["kind"] != "memory-link-state" or old.get("workspace_path") != L.normalized(ws)):
        raise ValueError("The snapshot does not describe links in this workspace")
    print("SNAPSHOT_MEMORY_FILES", old.get("memory_files", []))
    build(ws)
    print("MEMORY_LINK_REBUILT")


def restore_memory(ws, archive, preview, apply):
    root = backups.directory(ws, MEMORY)
    recovered = backups.recover(ws, root, archive, preview)
    if apply:
        merge_memory(recovered, root, "restored")
        build(ws)
        print("MEMORY_RESTORE_APPLIED", root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--workspace")
    scope.add_argument("--all", action="store_true")
    actions = parser.add_mutually_exclusive_group()
    for flag in ("status", "probe", "check", "snapshot", "backup-memory"):
        actions.add_argument("--" + flag, action="store_true")
    actions.add_argument("--why", choices=(MEMORY, ALIAS))
    actions.add_argument("--restore")
    actions.add_argument("--restore-memory", metavar="ZIP")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--restore-preview", action="store_true")
    mode.add_argument("--apply-memory", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--initialized-only", action="store_true")
    selection.add_argument("--include-uninitialized", action="store_true")
    args = parser.parse_args()
    if (args.restore_preview or args.apply_memory) and not args.restore_memory:
        parser.error("Restore options require --restore-memory")
    if (args.restore or args.restore_memory) and not args.workspace:
        parser.error("Restore requires one workspace")
    if args.include_uninitialized and not (args.probe and args.all):
        parser.error("--include-uninitialized requires --all --probe")
    if args.initialized_only and not args.all:
        parser.error("--initialized-only requires --all")
    try:
        names = C.list_workspaces()
        if args.workspace:
            name = os.path.basename(os.path.normpath(args.workspace))
            ws = os.path.join(C.WORKSPACE_ROOT, name)
            if name not in names or (os.path.isabs(args.workspace) and not L.same_target(args.workspace, ws)):
                raise ValueError("Unknown workspace: " + args.workspace)
            names = [name]
        elif (args.initialized_only or args.snapshot or args.backup_memory
              or (args.probe and not args.include_uninitialized)):
            names = [name for name in names if os.path.isdir(os.path.join(C.WORKSPACE_ROOT, name, MEMORY))]
        failed = 0
        for name in names:
            ws = os.path.join(C.WORKSPACE_ROOT, name)
            if not L.within(ws, C.WORKSPACE_ROOT):
                raise ValueError("Workspace escapes configured root: " + ws)
            if args.why:
                why(ws, args.why)
            elif args.restore:
                restore(ws, args.restore)
            elif args.restore_memory:
                restore_memory(ws, args.restore_memory, args.restore_preview, args.apply_memory)
            elif args.backup_memory:
                print("MEMORY_BACKUP", backups.backup(ws, backups.directory(ws, MEMORY)))
            else:
                action = next((fn for fn in (status, probe, check, snapshot) if getattr(args, fn.__name__)), build)
                failed += action(ws) or 0
        return 1 if failed else 0
    except (OSError, ValueError, RuntimeError, BadZipFile) as error:
        print("ERROR:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
