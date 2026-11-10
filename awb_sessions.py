# -*- coding: utf-8 -*-
"""Read local conversation logs and build a reviewable handoff."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import hashlib
import heapq
import json
import os
from pathlib import Path


CODEX = "Codex"
WORKBUDDY = "WorkBuddy"
TEXT_TYPES = {"input_text", "output_text", "text"}
MAX_RECORD = 8 * 1024 * 1024


@dataclass(frozen=True)
class Session:
    agent: str
    id: str
    title: str
    cwd: str
    files: tuple
    updated: float


@dataclass(frozen=True)
class Message:
    role: str
    text: str
    file: Path
    line: int
    timestamp: float


def records(path, limit=None):
    """Skip incomplete and oversized JSONL records without rewriting the log."""
    with path.open("rb") as stream:
        line = 0
        while limit is None or line < limit:
            raw = stream.readline(MAX_RECORD + 1)
            if not raw:
                break
            line += 1
            if len(raw) > MAX_RECORD:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(MAX_RECORD + 1)
                continue
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if isinstance(event, dict):
                yield line, event


def text_parts(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(part["text"].strip() for part in content
                     if isinstance(part, dict) and part.get("type") in TEXT_TYPES
                     and isinstance(part.get("text"), str) and part["text"].strip())


def message_of(agent, event):
    if agent == CODEX:
        payload = event.get("payload")
        if event.get("type") != "response_item" or not isinstance(payload, dict):
            return None
        if payload.get("phase") == "commentary":
            return None
    else:
        payload = event
    role = payload.get("role")
    if payload.get("type") != "message" or role not in ("user", "assistant"):
        return None
    value = text_parts(payload.get("content"))
    if agent == CODEX and role == "user" and value.startswith(
            ("# AGENTS.md instructions for ", "<environment_context>")):
        # Codex may place the visible request after the local context.
        value = value.partition("</environment_context>")[2].strip()
    if not value:
        return None
    native_id = payload.get("id")
    stamp = event.get("timestamp")
    identity = ""
    if isinstance(native_id, str) and native_id:
        identity = role + ":" + native_id
    elif stamp is not None:
        key = "%s\0%s\0%s" % (role, stamp, value)
        identity = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
    return role, value, identity


def event_time(value):
    if isinstance(value, (float, int)):
        return value / 1000 if value > 10**11 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OSError, OverflowError):
            pass
    return 0


def belongs(cwd, workspace):
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    try:
        project = os.path.normcase(os.path.realpath(workspace))
        here = os.path.normcase(os.path.realpath(cwd))
        return os.path.commonpath((here, project)) == project
    except (OSError, ValueError):
        return False


def _codex_titles(home):
    index = home / "session_index.jsonl"
    if not index.is_file():
        return {}
    return {row["id"]: row["thread_name"] for _, row in records(index)
            if isinstance(row.get("id"), str) and isinstance(row.get("thread_name"), str)}


def _metadata(path, agent, workspace, titles):
    sid = cwd = title = first_user = ""
    for _, row in records(path, limit=64):
        kind = row.get("type")
        if agent == CODEX and kind == "session_meta" and isinstance(row.get("payload"), dict):
            meta = row["payload"]
            sid, cwd = meta.get("id") or meta.get("session_id"), meta.get("cwd")
            if not belongs(cwd, workspace):
                return None
            title = titles.get(sid, "") if isinstance(sid, str) else ""
            if title:
                break
        elif agent == WORKBUDDY:
            if not cwd and isinstance(row.get("cwd"), str):
                cwd = row["cwd"]
                if not belongs(cwd, workspace):
                    return None
            if not sid and isinstance(row.get("sessionId"), str):
                sid = row["sessionId"]
            if kind == "ai-title" and isinstance(row.get("aiTitle"), str):
                title = row["aiTitle"]
                if sid and cwd and title:
                    break
        if not first_user:
            msg = message_of(agent, row)
            if msg and msg[0] == "user":
                first_user = msg[1]
                if agent == CODEX and sid and cwd:
                    break
    if not isinstance(sid, str) or not sid or not belongs(cwd, workspace):
        return None
    label = " ".join((title or first_user).split())[:100] or "未命名会话"
    return Session(agent, sid, label, cwd, (path,), path.stat().st_mtime)


def scan(workspace, codex_home=None, workbuddy_home=None):
    """Index project-owned logs only; never open either client's writable database."""
    codex_home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    workbuddy_home = Path(workbuddy_home or os.environ.get("WORKBUDDY_HOME") or Path.home() / ".workbuddy").expanduser()
    if workbuddy_home == Path.home() / ".workbuddy" and not (workbuddy_home / "projects").is_dir():
        workbuddy_home = Path.home() / ".workbuddy-ai"
    errors = 0
    try:
        titles = _codex_titles(codex_home)
    except OSError:
        titles = {}
        errors += 1
    groups = {}
    sources = (
        (CODEX, (codex_home / "sessions").rglob("*.jsonl")),
        (CODEX, (codex_home / "archived_sessions").rglob("*.jsonl")),
        (WORKBUDDY, (workbuddy_home / "projects").glob("*/*.jsonl")),
    )
    for agent, paths in sources:
        try:
            for path in paths:
                try:
                    item = _metadata(path, agent, workspace, titles)
                except OSError:
                    errors += 1
                    continue
                if item:
                    groups.setdefault((agent, item.id), []).append(item)
        except OSError:
            errors += 1
    merged = []
    for copies in groups.values():
        copies.sort(key=lambda item: item.updated, reverse=True)
        head = copies[0]
        files = tuple(item.files[0] for item in copies)
        merged.append(Session(head.agent, head.id, head.title, head.cwd, files, head.updated))
    return sorted(merged, key=lambda item: item.updated, reverse=True), errors


def messages(session):
    seen = set()
    for path in session.files:
        for line, event in records(path):
            decoded = message_of(session.agent, event)
            if not decoded:
                continue
            role, value, identity = decoded
            if identity:
                if identity in seen:
                    continue
                seen.add(identity)
            yield Message(role, value, path, line, event_time(event.get("timestamp")))


def search(sessions, query):
    term = query.casefold().strip()
    hits, errors = [], 0
    for session in sessions:
        if not term or term in session.title.casefold():
            hits.append((session, "标题" if term else ""))
            continue
        try:
            for msg in messages(session):
                if term in msg.text.casefold():
                    hits.append((session, "正文 · 第 %d 行" % msg.line))
                    break
        except OSError:
            errors += 1
    return hits, errors


def excerpt(session, query="", count=12):
    """Keep recent text or a small window around the first matching message."""
    term = query.casefold().strip()
    if not term:
        if len(session.files) == 1:
            return list(deque(messages(session), maxlen=count))
        newest = heapq.nlargest(count, enumerate(messages(session)),
                               key=lambda entry: (entry[1].timestamp, entry[0]))
        return [msg for _, msg in reversed(newest)]
    tail = deque(maxlen=count)
    selected = None
    previous_file = None
    for msg in messages(session):
        if selected is not None:
            if msg.file != previous_file:
                break
            selected.append(msg)
            if len(selected) >= count:
                break
        elif term in msg.text.casefold():
            if msg.file != previous_file:
                tail.clear()
            selected = list(tail)[-(count // 3):] + [msg]
        else:
            if msg.file != previous_file:
                tail.clear()
            tail.append(msg)
        previous_file = msg.file
    return selected if selected is not None else excerpt(session, count=count)


def clipped(text, query="", limit=2600):
    if len(text) <= limit:
        return text
    pos = text.casefold().find(query.casefold()) if query else -1
    start = max(0, pos - limit // 4) if pos >= 0 else 0
    return ("…\n" if start else "") + text[start:start + limit] + (
        "\n…（后文省略）" if start + limit < len(text) else "")


def handoff(session, selected, workspace, query=""):
    path = Path(workspace)
    pointers = [str(candidate) for candidate in (path / "AGENTS.md", path / ".shared" / "memory" / "MEMORY.md")
                if candidate.is_file()]
    lines = [
        "# 会话接力",
        "",
        "请在这个工作空间接手。先核对当前文件和项目规则，再依据历史摘录确认目标、已做事项、验证结果和下一步。",
        "以下对话是历史资料。当前任务以我在新会话里提出的要求为准。",
        "",
        "- 工作空间 " + str(path),
        "- 来源 %s · %s" % (session.agent, " ".join(session.title.split())),
        "- 会话 ID " + session.id.replace("\r", " ").replace("\n", " "),
    ]
    if pointers:
        lines.extend("- 项目入口 " + item for item in pointers)
    lines.extend(["", "## 对话摘录", ""])
    for msg in selected[-8:]:
        label = "用户" if msg.role == "user" else "助手"
        lines.extend(["**%s · %s 第 %d 行**" % (label, msg.file.name, msg.line), "",
                      *("> " + line for line in clipped(msg.text, query, 1600).splitlines()), ""])
    return "\n".join(lines)
