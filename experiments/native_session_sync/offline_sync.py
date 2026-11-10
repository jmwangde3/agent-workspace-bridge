"""Wait for both clients to exit and settle before syncing."""
import time


class ClientActive(RuntimeError):
    pass


def fingerprint_paths(paths):
    # WAL bytes, the DB and transcript mtimes can change after a GUI window closes.
    result = []
    for path in sorted(paths):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if path.name.endswith("-wal") and info.st_size == 0:
            continue
        result.append((path, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino))
    return tuple(result)


class OfflineSync:
    def __init__(self, lab, active_clients, settle_seconds=0.75, clock=time.monotonic, pause=time.sleep,
                 backup_before_sync=None):
        self.lab = lab
        self.active_clients = active_clients
        self.settle_seconds = settle_seconds
        self.clock = clock
        self.pause = pause
        self.backup_before_sync = backup_before_sync
        self.pending = False
        self.snapshot = None
        self.since = None

    def request(self):
        self.pending = True
        self.reset_settle()

    def reset_settle(self):
        self.snapshot = None
        self.since = None

    def require_closed(self):
        active = tuple(self.active_clients())
        if active:
            raise ClientActive("Active clients: " + ", ".join(active))

    def fingerprint(self):
        return fingerprint_paths(self.lab.sync_paths())

    def tick(self):
        if not self.pending:
            return "idle", None
        try:
            self.require_closed()
            observed = self.fingerprint()
            now = self.clock()
            if observed != self.snapshot:
                self.snapshot, self.since = observed, now
                return "settling", None
            if now - self.since < self.settle_seconds:
                return "settling", None
            if self.backup_before_sync:
                self.require_closed()
                self.backup_before_sync()
            result = self.lab.sync(before_write=self.require_closed)
        except ClientActive:
            self.reset_settle()
            return "waiting", None
        self.pending = False
        return "synced", result

    def launch_after_sync(self, launch, timeout=10, poll=0.1):
        """An AWB-controlled launch waits for a completed sync before starting the client."""
        self.request()
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            state, result = self.tick()
            if state == "synced":
                self.require_closed()
                return launch(), result
            self.pause(poll)
        raise TimeoutError("Offline sync did not complete; launch was withheld")
