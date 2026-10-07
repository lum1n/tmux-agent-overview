import json
import os
import socket
import stat
import time
from datetime import datetime

from .model import KINDS, STATES, clean


class Watcher:
    def __init__(self):
        self.sock = None
        self.path = ""
        self.buffer = b""
        self.records = {}
        self.quota = {}
        self.health = "standalone"
        self.retry = 0
        self.last_seen = 0
        self.last_ping = 0

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None
        self.buffer = b""
        self.records.clear()
        self.quota.clear()
        self.last_seen = 0

    def event(self, obj):
        if not isinstance(obj, dict) or obj.get("v") != 1:
            raise ValueError("unsupported watcher protocol")
        typ = obj.get("type")
        if typ == "snapshot":
            rows = obj.get("agents")
            if not isinstance(rows, list):
                raise ValueError("invalid snapshot")
            self.records.clear()
            for row in rows:
                self.row(row)
            self.health = "shared watcher"
        elif typ in ("state", "unbound"):
            self.row(obj)
        elif typ == "gone":
            self.records.pop((obj.get("session"), obj.get("window")), None)
        elif typ == "error":
            self.records.clear()
            self.health = "watcher error"
        elif typ == "quota":
            self.quota_row(obj)
        # Newer watchers may add event types; ignoring them keeps states flowing.

    def quota_row(self, obj):
        # Quota is optional: a malformed reading is dropped, never fatal.
        kind, windows, plan = obj.get("kind"), obj.get("windows"), obj.get("plan")
        if kind not in KINDS or not isinstance(windows, list) or not 0 < len(windows) <= 8:
            return
        parsed = []
        for window in windows:
            if not isinstance(window, dict):
                return
            name, used, resets = window.get("label"), window.get("usedPercent"), window.get("resetsAt")
            if not isinstance(name, str) or type(used) not in (int, float) or not 0 <= used <= 100:
                return
            reset = None
            if isinstance(resets, str) and len(resets) <= 64:
                try:
                    stamp = datetime.fromisoformat(resets.replace("Z", "+00:00"))
                    reset = stamp.timestamp() if stamp.tzinfo else None
                except (ValueError, OverflowError):
                    reset = None
            parsed.append((clean(name)[:16], float(used), reset))
        plan = clean(plan)[:24] if isinstance(plan, str) else ""
        self.quota[kind] = (plan, tuple(parsed), obj.get("stale") is True)

    def row(self, row):
        if not isinstance(row, dict):
            raise ValueError("invalid watcher record")
        name, index, kind = row.get("session"), row.get("window"), row.get("kind")
        if not isinstance(name, str) or len(name) > 256 or type(index) is not int or index < 0 or kind not in KINDS:
            raise ValueError("invalid watcher identity")
        state = row.get("state", "unknown")
        if state not in STATES:
            raise ValueError("invalid watcher state")
        self.records[(name, index)] = (kind, "unknown" if row.get("unbound") else state)

    def poll(self, path):
        if path != self.path:
            self.close()
            self.path = path
            self.retry = 0
        if not path:
            self.health = "standalone"
            return
        if not self.sock:
            if time.monotonic() < self.retry:
                return
            sock = None
            try:
                info = os.stat(path)
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("unsafe watcher socket")
                sock = socket.socket(socket.AF_UNIX)
                sock.settimeout(.2)
                sock.connect(path)
                sock.setblocking(False)
                self.sock = sock
                self.health = "shared watcher"
                self.last_seen = time.monotonic()
                self.last_ping = self.last_seen
            except (OSError, ValueError):
                if sock:
                    sock.close()
                self.health = "watcher unavailable"
                self.retry = time.monotonic() + 3
                return
        try:
            if time.monotonic() - self.last_seen > 40:
                raise OSError("watcher heartbeat missing")
            if time.monotonic() - self.last_ping > 20:
                self.sock.sendall(b'{"cmd":"ping"}\n')
                self.last_ping = time.monotonic()
            for _ in range(32):
                try:
                    data = self.sock.recv(65536)
                except BlockingIOError:
                    break
                if not data:
                    raise OSError("watcher disconnected")
                self.last_seen = time.monotonic()
                self.buffer += data
                if len(self.buffer) > 1024 * 1024:
                    raise ValueError("watcher frame too large")
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    self.event(json.loads(line))
        except (OSError, ValueError, UnicodeError):
            self.close()
            self.health = "watcher unavailable"
            self.retry = time.monotonic() + 3

    def state(self, agent, agents):
        if not agent.active or sum(a.window == agent.window for a in agents) != 1:
            return None
        row = self.records.get((agent.session_name, int(agent.index)))
        return row[1] if row and row[0] == agent.kind else None
