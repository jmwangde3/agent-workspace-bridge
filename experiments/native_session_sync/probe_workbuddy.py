"""Run the installed WorkBuddy session backend against a synthetic profile."""
import json
from collections import deque
from contextlib import suppress
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from native_sync import Lab, read_messages


def isolated_env(lab, executable):
    home = lab.local("process-home")
    paths = {name: home / name for name in ("roaming", "local", "temp", "electron")}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    # Only pass OS launch variables to this isolated process.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                              "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}}
    resources = executable.parent / "resources"
    env.update(ELECTRON_RUN_AS_NODE="1",
               NODE_OPTIONS='--require="' + Path(__file__).resolve().with_name("loopback_only.cjs").as_posix() + '"',
               HOME=str(home), USERPROFILE=str(home),
               APPDATA=str(paths["roaming"]), LOCALAPPDATA=str(paths["local"]),
               TEMP=str(paths["temp"]), TMP=str(paths["temp"]),
               WORKBUDDY_CONFIG_DIR=str(lab.workbuddy), CODEBUDDY_CONFIG_DIR=str(lab.workbuddy),
               WORKBUDDY_DATA_DIR=str(lab.workbuddy), WORKBUDDY_USER_DATA_DIR=str(paths["electron"]),
               WORKBUDDY_APP_PATH=str(resources / "app.asar"), WORKBUDDY_RESOURCES_PATH=str(resources),
               WORKBUDDY_APP_VERSION="5.9.3", WORKBUDDY_IS_PACKAGED="1",
               WORKBUDDY_FS_PROTECTION_ROLE="daemon",
               WORKBUDDY_PAC_RESOLVER="off")
    return env


class WorkBuddy:
    def __init__(self, lab, executable):
        self.lab = lab
        self.executable = Path(executable).resolve()
        self.env = isolated_env(lab, self.executable)
        self.queue = queue.Queue()
        self.events = deque(maxlen=600)
        self.parent_calls = set()
        self.serial = 0

    def __enter__(self):
        self.log_path = self.lab.local("workbuddy-native.stderr")
        entry = self.executable.parent / "resources" / "app.asar" / "main" / "daemon-app-server-entry.js"
        with self.log_path.open("wb") as log:
            self.proc = subprocess.Popen(
                [str(self.executable), str(entry), "--stdio"],
                env=self.env, cwd=self.lab.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        threading.Thread(target=self.receive, daemon=True).start()
        try:
            deadline = time.monotonic() + 20
            while self.next_frame(deadline, "startup").get("type") != "ready":
                pass
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        with suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.proc.stdout.close()

    def receive(self):
        for line in self.proc.stdout:
            try:
                envelope, separator, result = line.partition("\x1f")
                frame = json.loads(envelope)
                if separator:
                    frame["result"] = json.loads(result)
                self.queue.put(frame)
            except ValueError:
                continue
        self.queue.put(None)

    def send(self, frame):
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, channel, *args):
        self.serial += 1
        rid = "awb-lab-%d" % self.serial
        self.send({"type": "rpc-request", "id": rid, "channel": channel, "args": args})
        deadline = time.monotonic() + 20
        while True:
            frame = self.next_frame(deadline, channel)
            if frame.get("type") not in ("rpc-response", "rpc-error") or frame.get("id") != rid:
                continue
            if frame.get("error"):
                raise RuntimeError("%s: %s" % (channel, frame["error"]))
            return frame.get("result")

    def next_frame(self, deadline, operation):
        while True:
            try:
                frame = self.queue.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as error:
                raise RuntimeError("Native WorkBuddy timed out on " + operation) from error
            if frame is None:
                raise RuntimeError("Native WorkBuddy exited before " + operation)
            if frame.get("type") != "rpc-request":
                if frame.get("type") == "rpc-event":
                    self.events.append(frame)
                return frame
            self.parent_calls.add(frame["channel"])
            result = {"desktopHost:resolveProxy": "DIRECT",
                      "desktopHost:getEffectiveProxyMode": "direct"}.get(frame["channel"])
            self.send({"type": "rpc-response", "id": frame["id"], "channel": frame["channel"],
                       "result": result})

    def wait_for_replay(self, markers, timeout=4, session_id=None):
        deadline = time.monotonic() + timeout
        while True:
            replay = json.dumps([item for item in self.events
                                 if not session_id or item.get("channel") == "session:event:" + session_id],
                                ensure_ascii=False)
            if all(marker in replay for marker in markers):
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                self.next_frame(deadline, "history replay")
            except RuntimeError:
                return False


def verify_pair(lab, executable):
    """Observe both installed runtimes close, then verify both native lists after sync."""
    from backup_lab import capture, verify
    from offline_sync import OfflineSync
    from probe_codex import Codex

    a = [["user", "Codex isolated marker 18743"], ["assistant", "Codex original answer 18743"]]
    b = [["user", "WorkBuddy isolated marker 65391"], ["assistant", "WorkBuddy original answer 65391"]]
    lab.seed("codex", "Codex A", a)
    lab.seed("workbuddy", "WorkBuddy B", b)
    running = {}
    archive = lab.local("before-pair-sync.zip")
    gate = OfflineSync(lab, lambda: [name for name, app in running.items() if app.proc.poll() is None],
                       backup_before_sync=lambda: capture(lab, archive))
    with Codex(lab.codex, lab.workspace) as codex:
        running["Codex"] = codex
        with WorkBuddy(lab, executable) as workbuddy:
            running["WorkBuddy"] = workbuddy
            assert len(codex.call("thread/list", {"limit": 10})["data"]) == 1
            assert len(workbuddy.call("session:list", {"userId": "awb-fixture-owner"})["agents"]) == 1
            gate.request()
            assert gate.tick()[0] == "waiting" and not archive.exists()
        assert gate.tick()[0] == "waiting" and not archive.exists()

    def reopen():
        assert verify(archive)
        with Codex(lab.codex, lab.workspace) as codex:
            listed = codex.call("thread/list", {"limit": 10})["data"]
            assert len(listed) == 2
            for item in lab.copies("codex").values():
                thread = codex.call("thread/resume", {"threadId": item.sid})["thread"]
                turns = thread["turns"]
                assert len(turns) == 1
                user, assistant = turns[0]["items"]
                assert [["user", user["content"][0]["text"]], ["assistant", assistant["text"]]] == item.messages
        with WorkBuddy(lab, executable) as workbuddy:
            listed = workbuddy.call("session:list", {"userId": "awb-fixture-owner"})["agents"]
            assert len(listed) == 2
            for item in lab.copies("workbuddy").values():
                loaded = workbuddy.call("session:load", item.sid,
                                        {"cwd": str(lab.workspace), "forceRendererHistoryReplay": True})
                assert loaded.get("rendererHistoryReplayComplete") is True, loaded
                assert workbuddy.wait_for_replay([text for _, text in item.messages], session_id=item.sid)
        return "PAIR_NATIVE_A_AND_B"

    result, stats = gate.launch_after_sync(reopen)
    assert stats["created"] == 2, stats
    assert lab.sync()["created"] == 0
    print(result, stats)
    print("VERIFIED_BACKUP", len(verify(archive)))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--require-replay", action="store_true")
    parser.add_argument("--pair", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / ("workbuddy-probe-" + uuid.uuid4().hex[:8])
    lab = Lab(root, create=True)
    if args.pair:
        verify_pair(lab, args.executable)
        print("PROBE_ROOT", root)
        raise SystemExit(0)
    expected = [["user", "AWB test marker 18743"], ["assistant", "Recorded 18743"]]
    sid = lab.seed("workbuddy", "AWB isolated fixture", expected)
    try:
        with WorkBuddy(lab, args.executable) as app:
            for channel, argv in (("deviceTransfer:listSessions", []),
                                  ("deviceTransfer:sessionFileDetails", [{"sessionIds": [sid]}]),
                                  ("session:list", [{"userId": "awb-fixture-owner"}]),
                                  ("session:get", [sid]),
                                  ("session:load", [sid, {"cwd": str(lab.workspace), "forceRendererHistoryReplay": True}])):
                try:
                    result = app.call(channel, *argv)
                    print(channel, type(result).__name__, "SID", sid in json.dumps(result),
                          "MARKER", "18743" in json.dumps(result))
                    if isinstance(result, dict):
                        print("FIELDS", list(result))
                        if channel == "session:load":
                            print("LOAD_STATE", result)
                except (RuntimeError, OSError) as error:
                    print(error)
                    if app.proc.poll() is not None:
                        break
            replay = app.wait_for_replay(("AWB test marker 18743", "Recorded 18743"))
            print("REPLAY_MATCH", replay,
                  "EVENT_CHANNELS", sorted({item.get("channel", "") for item in app.events}))
            export = lab.local("workbuddy", "device-transfer", "awb-export")
            exported = app.call("deviceTransfer:exportSession", {"sessionId": sid, "toDir": str(export)})
            print("EXPORT_FIELDS", list(exported), "PARENT_CALLS", sorted(app.parent_calls))
            if (export / "transcript.jsonl").is_file():
                assert read_messages(export / "transcript.jsonl", "workbuddy") == expected
                print("NATIVE_TRANSCRIPT_ROUNDTRIP", True)
                imported_id = str(uuid.uuid4())
                transcript = export / "transcript.jsonl"
                with transcript.open(encoding="utf-8") as handle:
                    records = [json.loads(line) for line in handle]
                with transcript.open("w", encoding="utf-8") as handle:
                    for record in records:
                        record["sessionId"] = imported_id
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                now = int(time.time() * 1000)
                row = {"id": imported_id, "cwd": str(lab.workspace), "title": "AWB installed test fixture",
                       "status": "completed", "createdAt": now, "updatedAt": now,
                       "lastActivityAt": now, "mode": "craft", "isPlayground": 0}
                status = app.call("deviceTransfer:installSession", {"row": row, "fromDir": str(export)})
                assert status.get("status") == "installed", status
                imported = app.call("session:get", imported_id)
                assert imported.get("id") == imported_id, imported
                copy_out = lab.local("workbuddy", "device-transfer", "awb-reexport")
                app.call("deviceTransfer:exportSession", {"sessionId": imported_id, "toDir": str(copy_out)})
                assert read_messages(copy_out / "transcript.jsonl", "workbuddy") == expected
                print("NATIVE_INSTALL_AND_REEXPORT", True)
            else:
                raise RuntimeError("WorkBuddy did not export the fixture transcript")
            if args.require_replay and not replay:
                raise RuntimeError("WorkBuddy did not replay both original messages")
    finally:
        print("PROBE_LOG", lab.local("workbuddy-native.stderr"))
