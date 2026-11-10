"""Native Codex text-turn fixture for the offline sync lab."""
from datetime import datetime, timezone
import json
import os
import re
import tempfile
import uuid


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event(at, kind, payload):
    return {"timestamp": iso(at), "type": kind, "payload": payload}


def turn_records(sid, first_turn, messages, at, first_ordinal=None):
    records = _turn_records(sid, first_turn, messages, at, first_ordinal is not None)
    for index, record in enumerate(records):
        if first_ordinal is not None:
            record["ordinal"] = first_ordinal + index
        yield record


def _turn_records(sid, first_turn, messages, at, paginated):
    for n in range(0, len(messages), 2):
        user, assistant = messages[n:n + 2]
        assert user[0] == "user" and assistant[0] == "assistant"
        turn = str(uuid.uuid5(uuid.UUID(sid), "turn-%d" % (first_turn + n // 2)))
        now = at + n / 1000
        start = {"type": "task_started", "turn_id": turn, "root_turn_id": turn,
                 "started_at": int(now), "collaboration_mode_kind": "default"}
        yield event(now, "event_msg", start)
        for role, value in (user, assistant):
            mid = str(uuid.uuid5(uuid.UUID(turn), role))
            part = {"type": "input_text" if role == "user" else "output_text", "text": value}
            payload = {"type": "message", "id": mid, "role": role, "content": [part]}
            if role == "assistant":
                payload["phase"] = "final_answer"
            yield event(now, "response_item", payload)
            if paginated:
                content = [{"type": "text", "text": value, "text_elements": []}] if role == "user" else [
                    {"type": "Text", "text": value}]
                item = {"type": "UserMessage" if role == "user" else "AgentMessage", "id": mid, "content": content}
                yield event(now, "event_msg", {"type": "item_completed", "thread_id": sid,
                                              "turn_id": turn, "item": item})
            elif role == "user":
                yield event(now, "event_msg", {"type": "user_message", "message": value, "images": [],
                                               "local_images": [], "text_elements": []})
            else:
                yield event(now, "event_msg", {"type": "agent_message", "message": value, "phase": "final_answer"})
        yield event(now, "event_msg", {"type": "task_complete", "turn_id": turn, "root_turn_id": turn,
                                      "last_agent_message": assistant[1], "started_at": int(now),
                                      "completed_at": int(now), "duration_ms": 0})


def encode(events):
    return "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events)


def desktop_page_context(record, payload, turn):
    """Recognize Codex Desktop's page snapshot frames, which have no tool invocation."""
    name = payload.get("name")
    metadata = record.get("metadata")
    attribution = payload.get("internal_chat_message_metadata_passthrough")
    return (
        payload.get("type") == "function_call_output"
        and payload.get("namespace") == "agent"
        and "call_id" not in payload
        and isinstance(name, str)
        and (name == "codex_apps_open_page" or re.fullmatch(r"codex_apps_selected_page_part_\d+_of_\d+", name))
        and isinstance(metadata, dict)
        and metadata.get("client_authored") is False
        and isinstance(attribution, dict)
        and attribution.get("turn_id") == turn
    )


def paginated_messages(meta, lines):
    """Read user-visible items, keeping provider and workspace prompts local."""
    if meta.get("type") != "session_meta" or meta.get("ordinal") != 0:
        raise ValueError("Unsupported Codex pagination header")
    messages = []
    current = []
    turn = None
    for ordinal, line in enumerate(lines, 1):
        record = json.loads(line)
        if record.get("ordinal") != ordinal:
            raise ValueError("Nonconsecutive Codex rollout ordinal %d" % ordinal)
        kind, payload = record.get("type"), record.get("payload", {})
        event_type = payload.get("type")
        if kind in ("world_state", "turn_context", "token_usage_record"):
            continue
        if kind == "response_item":
            if event_type in ("message", "reasoning", "additional_tools"):
                continue
            if turn and not current and desktop_page_context(record, payload, turn):
                continue
            raise ValueError("Unsupported Codex model item at ordinal %d" % ordinal)
        if kind != "event_msg":
            raise ValueError("Unsupported Codex rollout event at ordinal %d" % ordinal)
        if event_type == "task_started" and turn is None:
            turn = payload.get("turn_id")
        elif event_type == "task_complete" and turn and payload.get("turn_id") == turn:
            if [role for role, _ in current] != ["user", "assistant"]:
                raise ValueError("Unsupported Codex turn at ordinal %d" % ordinal)
            messages.extend(current)
            current = []
            turn = None
        elif event_type == "item_completed" and turn and payload.get("turn_id") == turn:
            item = payload.get("item", {})
            item_type = item.get("type")
            if item_type == "Reasoning":
                continue
            role, block_type = {"UserMessage": ("user", "text"), "AgentMessage": ("assistant", "Text")}.get(
                item_type, (None, None))
            blocks = item.get("content")
            if (role is None or not isinstance(blocks, list) or len(blocks) != 1
                    or not isinstance(blocks[0], dict) or blocks[0].get("type") != block_type
                    or blocks[0].get("text_elements") or not isinstance(blocks[0].get("text"), str)):
                raise ValueError("Unsupported Codex conversation item at ordinal %d" % ordinal)
            current.append([role, blocks[0]["text"]])
        elif event_type not in ("thread_settings_applied", "token_count"):
            raise ValueError("Unsupported Codex turn event at ordinal %d" % ordinal)
    if turn is not None or current:
        raise ValueError("Codex has an unfinished turn")
    return messages


def appended_records(content, sid, first_turn, messages, at):
    if not content.endswith("\n"):
        raise ValueError("Cannot append to an incomplete Codex rollout")
    first, _, tail = content.lstrip("\ufeff").partition("\n")
    mode = json.loads(first)["payload"].get("history_mode", "legacy")
    ordinal = None
    if mode == "paginated":
        last = tail.rstrip("\r\n").rsplit("\n", 1)[-1]
        ordinal = json.loads(last)["ordinal"] + 1
    elif mode != "legacy":
        raise ValueError("Unsupported Codex history mode")
    return encode(turn_records(sid, first_turn, messages, at, ordinal))


def path_for(home, sid, at):
    when = datetime.fromtimestamp(at, timezone.utc)
    dest = home / "sessions" / when.strftime("%Y") / when.strftime("%m") / when.strftime("%d")
    name = "rollout-%s-%s.jsonl" % (when.strftime("%Y-%m-%dT%H-%M-%S"), sid)
    return dest / name


def create(home, sid, workspace, messages, at, history_mode="legacy", model_provider="openai", account=None):
    path = path_for(home, sid, at)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = event(at, "session_meta", {"id": sid, "timestamp": iso(at), "cwd": str(workspace),
                                     "originator": "codex_cli_rs", "cli_version": "0.167.0",
                                     "source": "cli", "model_provider": model_provider, "history_mode": history_mode})
    ordinal = None
    if history_mode == "paginated":
        meta["ordinal"] = 0
        ordinal = 1
    if account:
        meta["payload"]["creator_account_id"] = account
    fd, temp = tempfile.mkstemp(prefix=".awb-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encode([meta, *turn_records(sid, 0, messages, at, ordinal)]))
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            os.rename(temp, path)  # Windows rename fails if the destination already exists.
        else:
            os.link(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return path
