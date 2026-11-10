"""Shared, conservative directory-link operations. Never delete real content."""
import os
import subprocess

REPARSE_POINT = 0x400


def is_link(path):
    if os.path.islink(path):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def normalized(path):
    return os.path.normcase(os.path.realpath(path))


def same_target(path, target):
    return normalized(path) == normalized(target)


def within(path, root):
    try:
        return os.path.commonpath((normalized(path), normalized(root))) == normalized(root)
    except ValueError:
        return False


def child_path(root, relative):
    path = os.path.abspath(os.path.join(root, relative))
    if os.path.isabs(relative) or not within(os.path.dirname(path), root):
        raise ValueError("Path escapes workspace: %s" % relative)
    return path


def remove_link(path):
    if not os.path.lexists(path):
        return
    if not is_link(path):
        raise ValueError("Refusing to remove real content: %s" % path)
    if os.path.islink(path):
        os.unlink(path)
    else:
        os.rmdir(path)


def create_link(target, out):
    try:
        os.symlink(target, out, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        if any(c in str(target) + str(out) for c in ('%', '"', '\n', '\r')):
            raise ValueError("Unsupported junction path: %s" % out)
        result = subprocess.run(
            'cmd.exe /d /v:off /s /c "mklink /J "%s" "%s""' % (out, target),
            capture_output=True, text=True, encoding="gbk", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise OSError("mklink failed: " + (result.stdout + result.stderr).strip())


def ensure_link(target, out):
    """Return True only if changed; roll back a replaced alias on failure."""
    target, out = os.path.abspath(target), os.path.abspath(out)
    if not os.path.isdir(target):
        raise FileNotFoundError("Missing link target: %s" % target)
    if os.path.lexists(out) and not is_link(out):
        raise FileExistsError("Real content blocks link: %s" % out)
    if is_link(out) and same_target(out, target):
        return False
    previous = os.readlink(out) if is_link(out) else None
    if previous is not None:
        if not os.path.isabs(previous):
            previous = os.path.join(os.path.dirname(out), previous)
        remove_link(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        create_link(target, out)
    except Exception:
        if previous is not None and not os.path.lexists(out):
            create_link(previous, out)
        raise
    return True
