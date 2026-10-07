import json
import os
import posixpath
import re
import selectors
import shutil
import signal
import subprocess
import time
import threading
from datetime import datetime

from .model import Agent, KINDS, STATES, clean, parse_quota

REFERENCE = re.compile(r"hive-agent-v1\.[A-Za-z0-9_-]+")
FAILURES = {"auth", "offline", "timeout", "cancelled", "unavailable", "discovery",
            "protocol", "output_limit", "stale", "gone"}


class HiveError(RuntimeError):
    pass


def text(value, limit=16384):
    if not isinstance(value, str):
        raise HiveError("Hive response invalid")
    try:
        if len(value.encode("utf-8")) > limit:
            raise HiveError("Hive response invalid")
    except UnicodeError as exc:
        raise HiveError("Hive response invalid") from exc
    return value


def identifier(value, pattern, limit=128):
    value = text(value, limit)
    if not re.fullmatch(pattern, value):
        raise HiveError("Hive response invalid")
    return value


def reference(value):
    return identifier(value, REFERENCE, 4096)


def array(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise HiveError("Hive response invalid")
    return value


def object_value(value):
    if not isinstance(value, dict):
        raise HiveError("Hive response invalid")
    return value


def failure(value):
    value = object_value(value)
    if not isinstance(value.get("code"), str) or value["code"] not in FAILURES:
        raise HiveError("Hive response invalid")
    text(value.get("message"), 1024)
    return value["code"]


def envelope(value):
    value = object_value(value)
    if type(value.get("version")) is not int or value["version"] != 1:
        raise HiveError("Hive API incompatible")
    return value


def observation_age(value):
    if value is None:
        return 0
    try:
        stamp = datetime.fromisoformat(text(value, 64).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise HiveError("Hive timestamp invalid")
        return max(0, time.time() - stamp.timestamp())
    except (ValueError, OverflowError) as exc:
        raise HiveError("Hive timestamp invalid") from exc


def bounded_json(command, timeout, partial=False, cancel=None):
    limit = 8 * 1024 * 1024
    try:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, "out")
                    selector.register(process.stderr, selectors.EVENT_READ, "err")
                    buffers = {"out": bytearray(), "err": bytearray()}
                    deadline = time.monotonic() + timeout
                    while selector.get_map():
                        if cancel is not None and cancel.is_set():
                            raise HiveError("Hive operation cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise HiveError("Hive operation timed out")
                        for key, _ in selector.select(min(remaining, .2)):
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            buffers[key.data].extend(data)
                            if len(buffers[key.data]) > (limit if key.data == "out" else 8192):
                                raise HiveError("Hive output exceeded limit")
                    code = process.wait(timeout=max(.01, deadline - time.monotonic()))
                if code != 0 and not (partial and code == 2):
                    raise HiveError("Hive command unavailable")
                return envelope(json.loads(buffers["out"]))
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HiveError("Hive command unavailable") from exc
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HiveError("Hive response invalid") from exc


class Hive:
    def __init__(self, command):
        self.command = command
        self.ready = False
        self.cancelled = threading.Event()
        self.max_targets, self.max_lines, self.max_bytes = 16, 200, 65536

    @classmethod
    def detect(cls, tmux):
        mode = tmux.option("@agent-overview-hive") or "auto"
        if mode == "off":
            return None
        if mode not in ("auto", "on"):
            raise HiveError("Set @agent-overview-hive to auto, on, or off")
        command = shutil.which(tmux.option("@agent-overview-hive-bin") or "hive")
        if not command:
            if mode == "on":
                raise HiveError("Hive executable unavailable")
            return None
        return cls(command)

    def check(self):
        if self.ready:
            return
        result = bounded_json([self.command, "agents", "capabilities", "--json"], 3, cancel=self.cancelled)
        commands = array(result.get("commands"), 16)
        if not all(command in commands for command in ("list", "capture", "attach")):
            raise HiveError("Hive API incompatible")
        for name, ceiling in (("max_targets", 16), ("max_lines", 200), ("max_bytes", 65536)):
            value = result.get(name)
            if type(value) is not int or value < 1:
                raise HiveError("Hive API incompatible")
            setattr(self, name, min(value, ceiling))
        self.ready = True

    def inventory(self, current_socket):
        self.check()
        result = bounded_json([self.command, "agents", "list", "--json", "--timeout", "3s"], 55, True, self.cancelled)
        return parse_inventory(result, current_socket)

    def capture(self, agents):
        if not 1 <= len(agents) <= self.max_targets or len({agent.hive_id for agent in agents}) != len(agents):
            raise HiveError("Hive capture targets invalid")
        command = [self.command, "agents", "capture", "--json", "--timeout", "3s",
                   "--lines", str(self.max_lines)]
        for agent in agents:
            command.extend(["--id", reference(agent.hive_id)])
        result = bounded_json(command, 15, True, self.cancelled)
        return parse_captures(result, agents, self.max_bytes)


def parse_inventory(result, current_socket):
    agents, identities, references, hosts, unavailable = [], set(), {}, set(), 0
    for host in array(envelope(result).get("hosts"), 64):
        host = object_value(host)
        host_id = identifier(host.get("host"), r"[A-Za-z0-9][A-Za-z0-9._-]*")
        if host_id in hosts:
            raise HiveError("Hive response invalid")
        hosts.add(host_id)
        host_label = text(host.get("label"), 1024)
        if type(host.get("local")) is not bool or host.get("status") not in (
                "online", "degraded", "offline", "auth", "unavailable"):
            raise HiveError("Hive response invalid")
        servers = array(host.get("servers"), 64)
        if host.get("error") is not None:
            failure(host["error"])
            if host["status"] == "online":
                raise HiveError("Hive response invalid")
        if host["status"] not in ("online", "degraded"):
            failure(host.get("error"))
            unavailable += 1
            continue
        memberships, sockets, counted = 0, set(), unavailable
        for server in servers:
            server = object_value(server)
            socket = text(server.get("socket"), 1024)
            if not socket.startswith("/") or posixpath.normpath(socket) != socket or any(c in socket for c in "\0\r\n"):
                raise HiveError("Hive response invalid")
            if socket in sockets:
                raise HiveError("Hive response invalid")
            sockets.add(socket)
            rows = array(server.get("agents"), 4096)
            if server.get("status") == "unavailable":
                failure(server.get("error"))
                unavailable += 1
                continue
            if server.get("status") != "online":
                raise HiveError("Hive response invalid")
            if server.get("error") is not None:
                raise HiveError("Hive response invalid")
            generation = identifier(server.get("generation"), r"[1-9][0-9]*:[1-9][0-9]*")
            # Optional and per-host-account; bad readings are ignored, not fatal.
            quota = server.get("quota")
            readings = dict(filter(None, (parse_quota(q, "used_percent", "resets_at")
                                          for q in quota[:len(KINDS)] if isinstance(q, dict)))) if isinstance(quota, list) else {}
            same_server = host["local"] and os.path.realpath(socket) == os.path.realpath(current_socket)
            for row in rows:
                row = object_value(row)
                ref = reference(row.get("id"))
                pane = identifier(row.get("pane"), r"%[0-9]+")
                window = identifier(row.get("window"), r"@[0-9]+")
                session = identifier(row.get("session"), r"\$[0-9]+")
                if (row.get("host"), row.get("socket"), row.get("generation")) != (host_id, socket, generation):
                    raise HiveError("Hive response invalid")
                if row.get("kind") not in KINDS:
                    raise HiveError("Hive response invalid")
                members = array(row.get("memberships"), 4096)
                if not members:
                    raise HiveError("Hive response invalid")
                for member in members:
                    member = object_value(member)
                    identifier(member.get("session"), r"\$[0-9]+")
                    identifier(member.get("window"), r"@[0-9]+")
                    text(member.get("session_name"))
                    text(member.get("window_name"))
                primary = next((member for member in members if
                                (member["session"], member["window"]) == (session, window)), None)
                if primary is None:
                    raise HiveError("Hive response invalid")
                memberships += len(members)
                if memberships > 4096:
                    raise HiveError("Hive response invalid")
                path = text(row.get("path"))
                state, provenance = row.get("state", "unknown"), row.get("provenance", "unavailable")
                if state not in STATES or provenance not in ("shared", "unavailable"):
                    raise HiveError("Hive state response invalid")
                state_error = failure(row["state_error"]) if row.get("state_error") is not None else ""
                if state_error and provenance == "shared":
                    raise HiveError("Hive state response invalid")
                age = observation_age(row.get("observed_at"))
                if provenance == "shared" and age > 15:
                    state, provenance = "unknown", "unavailable"
                identity = (host_id, socket, generation, pane)
                if ref in references and references[ref] != identity:
                    raise HiveError("Hive reference identity mismatch")
                references[ref] = identity
                if identity in identities:
                    continue
                identities.add(identity)
                if same_server:
                    continue
                agents.append(Agent(pane, window, session, clean(primary["session_name"]),
                                    clean(primary["window_name"]), "", 0, False, clean(path), row["kind"],
                                    state=state, provenance=provenance, host=host_id, host_label=clean(host_label),
                                    hive_id=ref, socket=socket, generation=generation,
                                    state_error=state_error, state_age=age, quota=readings.get(row["kind"])))
                if len(agents) > 8192:
                    raise HiveError("Hive inventory exceeded limit")
        # Hive marks a host degraded because of its unavailable servers; count those, not both.
        if host["status"] == "degraded" and unavailable == counted:
            unavailable += 1
    return agents, f"Hive: {unavailable} unavailable host/server(s)" if unavailable else "Hive connected"


def parse_captures(result, agents, max_bytes=65536):
    requested = {agent.hive_id: agent.key for agent in agents}
    captures, seen = {}, set()
    for row in array(envelope(result).get("captures"), len(agents)):
        row = object_value(row)
        ref = reference(row.get("id"))
        if ref not in requested or ref in seen or type(row.get("truncated")) is not bool:
            raise HiveError("Hive capture response invalid")
        seen.add(ref)
        if row.get("error") is not None:
            code = failure(row["error"])
            if row.get("state") != "unknown" or row.get("provenance") != "unavailable" or text(row.get("text", ""), 0):
                raise HiveError("Hive capture response invalid")
            captures[requested[ref]] = (None, code, "unknown", "unavailable", "", 0)
        else:
            if row.get("state") not in STATES or row.get("provenance") not in ("heuristic", "shared"):
                raise HiveError("Hive capture response invalid")
            state_error = failure(row["state_error"]) if row.get("state_error") is not None else ""
            if state_error and row["provenance"] == "shared":
                raise HiveError("Hive capture response invalid")
            captures[requested[ref]] = (clean(text(row.get("text", ""), max_bytes)), "", row["state"],
                                      row["provenance"], state_error, observation_age(row.get("time")))
    if seen != set(requested):
        raise HiveError("Hive capture response incomplete")
    return captures
