"""Exercise Codex native import against an empty home without model requests."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading


class Codex:
    def __init__(self, home, workspace):
        self.workspace = str(workspace)
        env = dict(os.environ, CODEX_HOME=str(home))
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(key, None)
        self.proc = subprocess.Popen(
            [shutil.which("codex") or "codex", "app-server", "--listen", "stdio://"],
            env=env, cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.events = queue.Queue()
        threading.Thread(target=self._receive, daemon=True).start()
        self.serial = 0

    def __enter__(self):
        try:
            self.call("initialize", {"clientInfo": {"name": "awb_native_test", "version": "0.1"},
                                     "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.proc.stdout.close()

    def _receive(self):
        for line in self.proc.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue
        self.events.put(None)

    def send(self, item):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **item}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, method, params):
        self.serial += 1
        number = self.serial
        self.send({"id": number, "method": method, "params": params})
        while True:
            item = self.events.get(timeout=15)
            if item is None:
                raise RuntimeError("Codex closed during " + method)
            if item.get("id") != number or "method" in item:
                continue
            if "error" in item:
                raise RuntimeError("%s: %s" % (method, item["error"]))
            return item["result"]


if __name__ == "__main__":
    import tempfile
    import time
    import uuid
    import codex_rollout
    folder = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="codex-api-", dir=folder) as temp:
        root = Path(temp)
        home, workspace = root / "home", root / "workspace"
        home.mkdir()
        workspace.mkdir()
        sid = str(uuid.uuid4())
        codex_rollout.create(home, sid, workspace, [
            ("user", "AWB native fixture marker 18743"),
            ("assistant", "Recorded marker 18743"),
        ], time.time())
        with Codex(home, workspace) as api:
            listed = api.call("thread/list", {"limit": 10})["data"]
            print("PICKER_VISIBLE", any(item["id"] == sid for item in listed))
            for method, params in (("thread/read", {"threadId": sid, "includeTurns": True}),
                                   ("thread/resume", {"threadId": sid})):
                thread = api.call(method, params)["thread"]
                print(method, "TURNS", len(thread.get("turns", [])),
                      "MARKER_VISIBLE", "18743" in json.dumps(thread))
            logs = list((home / "sessions").rglob("*.jsonl"))
            print("ROLLOUT_COUNT", len(logs), "MARKER_PERSISTED",
                  any("18743" in f.read_text(encoding="utf-8") for f in logs))
