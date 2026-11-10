"""Inspect post-resume model requests using only a local fake model endpoint."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import tempfile
import threading

from native_sync import Lab
from probe_codex import Codex
from probe_workbuddy import WorkBuddy


HISTORY = [["user", "记住隔离测试标记 18743。"], ["assistant", "隔离测试标记是 18743。"]]
FOLLOW_UP = "前面记住的标记是什么？"
REPLY = "AWB_LOCAL_FIXTURE_18743"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.write_json({"object": "list", "data": []})

    def write_json(self, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_events(self, events):
        body = "".join(("event: %s\n" % name if name else "") + "data: " +
                       (json.dumps(data) if data != "[DONE]" else data) + "\n\n"
                       for name, data in events).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(content)
        if self.path.endswith("/count_tokens"):
            self.write_json({"input_tokens": 10})
            return
        self.server.requests.put((self.path, body))
        if self.path.endswith("/responses"):
            self.respond_responses(body)
        elif self.path.endswith("/messages"):
            self.anthropic(body)
        elif self.path.endswith("/chat/completions"):
            self.chat(body)
        else:
            self.send_error(404)

    def respond_responses(self, body):
        item = {"type": "message", "id": "msg_awb_fixture", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text", "text": REPLY, "annotations": []}]}
        envelope = {"id": "resp_awb_fixture", "object": "response", "model": body["model"],
                    "created_at": 1, "output": [item], "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
        started = dict(envelope, status="in_progress", output=[])
        events = [
            ("response.created", {"response": started}),
            ("response.output_item.added", {"output_index": 0, "item": dict(item, content=[], status="in_progress")}),
            ("response.output_text.delta", {"output_index": 0, "content_index": 0, "item_id": item["id"], "delta": REPLY}),
            ("response.output_item.done", {"output_index": 0, "item": item}),
            ("response.completed", {"response": envelope}),
        ]
        self.write_events([(name, dict(data, type=name, sequence_number=i))
                           for i, (name, data) in enumerate(events)])

    def anthropic(self, body):
        message = {"id": "msg_awb_fixture", "type": "message", "role": "assistant",
                   "model": body["model"], "content": [], "stop_reason": None,
                   "usage": {"input_tokens": 10, "output_tokens": 0}}
        events = [
            ("message_start", {"message": message}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": REPLY}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {}),
        ]
        if body.get("stream"):
            self.write_events([(name, dict(data, type=name)) for name, data in events])
        else:
            self.write_json(dict(message, content=[{"type": "text", "text": REPLY}], stop_reason="end_turn"))

    def chat(self, body):
        def chunk(delta, reason=None):
            return {"id": "chatcmpl-awb-fixture", "object": "chat.completion.chunk",
                    "created": 1, "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        self.write_events([(None, chunk({"role": "assistant", "content": REPLY})),
                           (None, chunk({}, "stop")), (None, "[DONE]")])


@contextmanager
def fixture_model():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = queue.Queue()
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port, server.requests
    finally:
        server.shutdown()
        server.server_close()


def visible_messages(body):
    result = []
    for item in body.get("input", body.get("messages", [])):
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            continue
        content = item.get("content")
        text = content if isinstance(content, str) else "\n".join(
            part["text"] for part in content or [] if isinstance(part, dict)
            and isinstance(part.get("text"), str))
        result.append([item["role"], text])
    return result


def assert_history(messages):
    history = [item for item in messages
               if not item[1].startswith(("# AGENTS.md instructions for ", "<environment_context>"))]
    assert history == HISTORY + [["user", FOLLOW_UP]], "User/assistant history differs from fixture"


def verify_codex(lab, origin, requests):
    (lab.workspace / "AGENTS.md").write_text("# Synthetic fixture project\n", encoding="utf-8")
    lab.local("codex", "config.toml").write_text(
        'model = "gpt-5.2"\nmodel_provider = "awb_fixture"\n'
        '[model_providers.awb_fixture]\nname = "AWB local fixture"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\nbase_url = "%s/v1"\n' % origin,
        encoding="utf-8",
    )
    baseline = lab.seed("codex", "Baseline", HISTORY)
    incoming = lab.wb_path(lab.seed("workbuddy", "Incoming", HISTORY))
    events = [json.loads(line) for line in incoming.read_text(encoding="utf-8").splitlines()]
    for event in events:
        if event.get("role") == "user":
            event["providerData"] = {"startsNewUserRequest": True}
            block = event["content"][0]
            block["text"] = (
                '<system-reminder data-role="user-context">WorkBuddy-only project context</system-reminder>\n'
                '<system-reminder data-role="additional-data">WorkBuddy-only clock</system-reminder>\n'
                f'<user_query>{block["text"]}</user_query>'
            )
    incoming.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    lab.sync()
    for copy in lab.copies("codex").values():
        # Bind the test rollout to the fake provider; Codex enforces provider identity on resume.
        head, tail = copy.path.read_text(encoding="utf-8").split("\n", 1)
        meta = json.loads(head)
        meta["payload"]["model_provider"] = "awb_fixture"
        copy.path.write_text(json.dumps(meta, ensure_ascii=False) + "\n" + tail, encoding="utf-8")
    imported = next(sid for sid in lab.copies("codex") if sid != baseline)
    captured = []
    for sid in (baseline, imported):
        with Codex(lab.codex, lab.workspace) as app:
            app.call("thread/resume", {"threadId": sid, "modelProvider": "awb_fixture", "model": "gpt-5.2"})
            app.call("turn/start", {"threadId": sid,
                                   "input": [{"type": "text", "text": FOLLOW_UP, "text_elements": []}]})
            path, body = requests.get(timeout=15)
            assert path.endswith("/responses"), path
            messages = visible_messages(body)
            assert_history(messages)
            while True:
                event = app.events.get(timeout=15)
                if event and event.get("method") == "turn/completed":
                    assert event["params"]["turn"]["status"] == "completed", event
                    break
            resumed = app.call("thread/read", {"threadId": sid, "includeTurns": True})["thread"]
            assert any(item.get("text") == REPLY for turn in resumed["turns"] for item in turn["items"])
            # Each native thread has its own wire IDs and cache routing key.
            payload = {key: value for key, value in body.items() if key not in ("client_metadata", "prompt_cache_key")}
            payload["input"] = [{key: value for key, value in item.items() if key != "id"} for item in body["input"]]
            captured.append(payload)
    assert captured[0] == captured[1], "Imported Codex request differs from its native baseline"
    print("CODEX_CONTEXT_MATCH", len(captured[1]["input"]), "LOCAL_COMPLETION_OK", flush=True)


def verify_workbuddy(lab, executable, origin, requests):
    from contextlib import closing
    import sqlite3

    model = "awb-native-fixture"
    lab.local("workbuddy", "models.json").write_text(json.dumps([
        {"id": model, "name": model, "vendor": "Custom", "url": origin + "/v1/chat/completions",
         "apiKey": "awb-fixture-key", "configured": True, "supportsToolCall": True,
         "supportsImages": False, "supportsReasoning": False, "maxInputTokens": 32000, "maxOutputTokens": 2048}
    ]), encoding="utf-8")
    baseline = lab.seed("workbuddy", "Baseline", HISTORY)
    lab.seed("codex", "Incoming", HISTORY)
    lab.sync()
    imported = next(sid for sid in lab.copies("workbuddy") if sid != baseline)
    with closing(sqlite3.connect(lab.wb_db)) as database, database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(sessions)")}
        if "model" not in columns:
            database.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
        database.execute("UPDATE sessions SET model=?", (model,))
    captured = []
    runtime = WorkBuddy(lab, executable)
    templates = runtime.executable.parent / "resources" / "app.asar.unpacked" / "resources" / "templates"
    runtime.env["WORKBUDDY_PROMPT_TEMPLATES_DIR"] = str(templates)
    with runtime as app:
        for sid in (baseline, imported):
            loaded = app.call("session:load", sid, {"cwd": str(lab.workspace), "forceRendererHistoryReplay": True})
            assert loaded.get("rendererHistoryReplayComplete"), loaded
            app.call("session:setModel", sid, model)
            try:
                outcome = app.call("session:sendMessage", sid, [{"type": "text", "text": FOLLOW_UP}])
            except RuntimeError as error:
                if "Authentication required" in str(error):
                    if not requests.empty():
                        raise AssertionError("Unexpected model traffic before WorkBuddy authentication")
                    # Runtime diagnostics can include the test HTTP server's generated password.
                    print("WORKBUDDY_SEND_AUTH_REQUIRED LOCAL_MODEL_REQUESTS=0", flush=True)
                    return False
                raise RuntimeError("WorkBuddy failed to send the fixture prompt; see isolated daemon.log") from None
            try:
                path, body = requests.get(timeout=3)
            except queue.Empty as error:
                raise RuntimeError("WorkBuddy did not request the local model: %r" % outcome) from error
            messages = visible_messages(body)
            assert_history(messages)
            assert app.wait_for_replay([REPLY], session_id=sid), outcome
            captured.append((messages, body.get("system"), body.get("tools")))
    assert captured[0] == captured[1], "Imported WorkBuddy request differs from its native baseline"
    print("WORKBUDDY_CONTEXT_MATCH", len(captured[1][0]), "LOCAL_COMPLETION_OK", flush=True)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbuddy", type=Path)
    parser.add_argument("--keep-fixture", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="context-probe-", dir=Path(__file__).resolve().parent,
                                     ignore_cleanup_errors=True, delete=not args.keep_fixture) as temporary:
        lab = Lab(Path(temporary) / "store", create=True)
        if args.keep_fixture:
            print("FIXTURE_ROOT", lab.root, flush=True)
        with fixture_model() as (origin, requests):
            if args.workbuddy:
                if not verify_workbuddy(lab, args.workbuddy, origin, requests):
                    raise SystemExit(2)
            else:
                verify_codex(lab, origin, requests)
