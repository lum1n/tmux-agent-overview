import concurrent.futures
import fcntl
import hashlib
import json
import os
import selectors
import shlex
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import inventory
from .model import capacity, card_content_size, card_frame, classify, label, native_layout
from .tmux import Tmux, TmuxError
from .watcher import Watcher

ENTRY = Path(__file__).resolve().parents[2] / "scripts" / "overview.py"
ACTIONS = {"open", "close", "focus", "zoom-source", "zoom", "next", "previous",
           "scroll-up", "scroll-down", "live", "escape", "help", "select"}


def runtime(socket_path):
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    name = "agent-overview-" + hashlib.sha256(socket_path.encode()).hexdigest()[:16]
    directory = parent / name
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("unsafe runtime directory")
    path = directory / "control.sock"
    if len(os.fsencode(path)) > 100:
        raise RuntimeError("runtime socket path too long; use a shorter TMPDIR")
    return directory, str(path)


@dataclass
class View:
    client: str
    session: str
    window: str
    origin: str
    panes: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)
    frames: dict = field(default_factory=dict)
    offsets: dict = field(default_factory=dict)
    page: int = 0
    selected: str = ""
    search: str = ""
    zoom_owner: tuple | None = None
    geometry: tuple = ()


class Controller:
    def __init__(self, tmux):
        self.tmux = tmux
        self.views = {}
        self.agents = []
        self.watcher = Watcher()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.discovery = None
        self.next_discovery = 0
        self.captures = {}
        self.cache = {}
        self.health = ""

    def restore(self, view):
        if view.zoom_owner:
            pane, window, epoch = view.zoom_owner
            try:
                current = self.tmux.display(pane, "#{window_zoomed_flag}\t#{pane_active}\t#{@agent-overview-epoch}")
                if current == f"1\t1\t{epoch}":
                    self.tmux.run("resize-pane", "-Z", "-t", pane)
                self.tmux.run("set-hook", "-wu", "-t", window, "after-resize-pane[9182]")
                self.tmux.run("set-option", "-wu", "-t", window, "@agent-overview-epoch")
            except TmuxError:
                self.health = "source zoom target changed; restoration skipped"
            view.zoom_owner = None

    def open(self, client):
        if client in self.views:
            view = self.views[client]
            self.restore(view)
            self.tmux.run("switch-client", "-c", client, "-t", view.session)
            return
        origin = self.tmux.client(client, "#{session_id}:#{window_id}.#{pane_id}")
        width, height = self.tmux.client(client, "#{client_width}|#{client_height}").split("|")
        # Control-mode clients have no terminal height; use their source window.
        if not height:
            height = self.tmux.display(origin, "#{window_height}")
        if not width.isdigit() or not height.isdigit() or int(width) < 2 or int(height) < 2:
            raise ValueError("client terminal is too small")
        name = "agent-overview-" + hashlib.sha256(client.encode()).hexdigest()[:12]
        # Never adopt an existing session solely because its name matches.
        existing = self.tmux.run("list-sessions", "-F", "#{session_name}").splitlines()
        if name in existing:
            owned = self.tmux.option("@agent-overview-owned", name)
            if owned != "1":
                raise RuntimeError("overview session name is already in use")
            self.tmux.run("kill-session", "-t", name)
        result = self.tmux.run("new-session", "-d", "-s", name, "-n", "agents",
                               "-x", width, "-y", height, "-P", "-F", "#{session_id} #{window_id} #{pane_id}", "sleep 10")
        session, window, bootstrap = result.split()
        pane = self.tmux.run("split-window", "-d", "-h", "-t", bootstrap, "-P", "-F", "#{pane_id}", "")
        self.tmux.run("kill-pane", "-t", bootstrap)
        view = View(client, session, window, origin, [pane])
        self.views[client] = view
        for option, value in (("@agent-overview-owned", "1"), ("key-table", "agent-overview"),
                              ("mouse", "on"), ("status-left", " AGENTS "), ("status-right", ""),
                              ("status-style", "bg=colour234,fg=colour252")):
            self.tmux.run("set-option", "-t", session, option, value)
        for option, value in (("@agent-overview-owned", "1"), ("pane-border-status", "off"),
                              ("pane-border-style", "fg=colour234,bg=colour234"),
                              ("pane-active-border-style", "fg=colour234,bg=colour234")):
            self.tmux.run("set-option", "-w", "-t", window, option, value)
        self.tmux.run("switch-client", "-c", client, "-t", session)
        self.next_discovery = 0

    def close(self, client, return_origin=True):
        view = self.views.pop(client)
        self.restore(view)
        if return_origin:
            try:
                self.tmux.run("switch-client", "-c", client, "-t", view.origin)
            except TmuxError:
                sessions = self.tmux.run("list-sessions", "-F", "#{session_id}\t#{@agent-overview-owned}")
                target = next((line.split("\t")[0] for line in sessions.splitlines() if not line.endswith("\t1")), None)
                if target:
                    self.tmux.run("switch-client", "-c", client, "-t", target)
        sessions = self.tmux.run("list-sessions", "-F", "#{session_id}").splitlines()
        if view.session in sessions:
            if self.tmux.option("@agent-overview-owned", view.session) != "1":
                raise RuntimeError("refusing to close a session no longer owned by overview")
            self.tmux.run("kill-session", "-t", view.session)

    def action(self, request):
        action, client = request.get("action"), request.get("client")
        if action not in ACTIONS or not isinstance(client, str) or not client or len(client) > 256:
            raise ValueError("invalid overview action")
        clients = self.tmux.run("list-clients", "-F", "#{client_name}").splitlines()
        if client not in clients:
            raise ValueError("client is no longer attached")
        if action == "open":
            self.open(client)
            return
        view = self.views.get(client)
        if not view:
            raise ValueError("overview is not open")
        if self.tmux.client(client, "#{session_id}") != view.session:
            raise ValueError("action requires the overview session")
        active = self.tmux.display(view.window, "#{pane_id}")
        if action == "select":
            index = request.get("index")
            if type(index) is not int or not 0 <= index < len(view.panes):
                raise ValueError("tile index out of range")
            self.tmux.run("select-pane", "-t", view.panes[index])
        elif action in ("focus", "zoom-source"):
            source = view.sources.get(active)
            agent = next((a for a in self.agents if a.pane == source), None)
            if not agent:
                raise ValueError("selected agent is no longer available")
            # Explicit membership disambiguates linked windows and prevents name matching.
            target = f"{agent.session}:{agent.window}.{agent.pane}"
            self.tmux.display(target, "#{pane_id}")
            if action == "zoom-source":
                for other in self.views.values():
                    if other.zoom_owner and other.zoom_owner[1] == agent.window:
                        self.restore(other)
                if self.tmux.display(target, "#{window_zoomed_flag}") != "1":
                    self.tmux.run("set-option", "-w", "-t", agent.window, "@agent-overview-epoch", "0")
                    hook = f'set-option -w -F -t {agent.window} @agent-overview-epoch "#{{e|+:#{{@agent-overview-epoch}},1}}"'
                    self.tmux.run("set-hook", "-w", "-t", agent.window, "after-resize-pane[9182]", hook)
                    self.tmux.run("select-pane", "-t", target)
                    self.tmux.run("resize-pane", "-Z", "-t", target)
                    epoch = self.tmux.display(target, "#{@agent-overview-epoch}")
                    view.zoom_owner = (agent.pane, agent.window, epoch)
            self.tmux.run("switch-client", "-Z", "-c", client, "-t", target)
        elif action == "close":
            self.close(client)
        elif action == "zoom":
            self.tmux.run("resize-pane", "-Z", "-t", active)
            view.frames.clear()
        elif action in ("next", "previous"):
            view.page = max(0, view.page + (1 if action == "next" else -1))
            view.geometry = ()
        elif action in ("scroll-up", "scroll-down", "live"):
            source = view.sources.get(active)
            if source:
                view.offsets[source] = 0 if action == "live" else max(0, min(180, view.offsets.get(source, 0) + (5 if action == "scroll-up" else -5)))
        elif action == "escape":
            if self.tmux.display(active, "#{window_zoomed_flag}") == "1":
                self.tmux.run("resize-pane", "-Z", "-t", active)
                view.frames.clear()
            elif view.search:
                self.tmux.run("set-option", "-t", view.session, "@agent-overview-search", "")
            else:
                self.close(client)
        elif action == "help":
            self.tmux.message(client, "Arrows/hjkl select | Enter focus | z preview zoom | Z focus+zoom | [] pages | / search | f filter | PgUp/PgDn scroll | g live | q close")

    def tick(self):
        now = time.monotonic()
        if self.discovery is None and now >= self.next_discovery:
            self.discovery = self.pool.submit(inventory, self.tmux)
            self.next_discovery = now + 2
        if self.discovery and self.discovery.done():
            try:
                agents = self.discovery.result()
                identities = {(a.session_name, a.index): (a.pane, a.kind) for a in agents}
                previous = {(a.session_name, a.index): (a.pane, a.kind) for a in self.agents}
                if identities != previous and self.watcher.sock:
                    self.watcher.records.clear()
                    try:
                        self.watcher.sock.sendall(b'{"cmd":"snapshot"}\n')
                    except OSError:
                        self.watcher.close()
                        self.watcher.health = "watcher unavailable"
                self.agents = agents
                self.health = ""
            except (TmuxError, OSError, RuntimeError, subprocess.TimeoutExpired):
                self.health = "discovery unavailable"
            finally:
                self.discovery = None
        self.watcher.poll(self.tmux.option("@agent_watcher_socket"))
        for pane, future in list(self.captures.items()):
            if future.done():
                try:
                    self.cache[pane] = (now, future.result()[-262144:])
                except TmuxError:
                    self.cache.pop(pane, None)
                del self.captures[pane]
        live = {a.pane for a in self.agents}
        self.cache = {p: data for p, data in self.cache.items() if p in live}
        for agent in self.agents:
            captured = self.cache.get(agent.pane)
            agent.state = classify(agent.kind, captured[1]) if captured and now - captured[0] <= 3 else "unknown"
            shared = self.watcher.state(agent, self.agents)
            if shared is not None:
                agent.state, agent.provenance = shared, "shared"
            else:
                agent.provenance = "heuristic"
        clients = {}
        for line in self.tmux.run("list-clients", "-F", "#{client_name}\t#{session_id}").splitlines():
            name, session = line.split("\t", 1)
            clients[name] = session
        for client, view in list(self.views.items()):
            if client not in clients:
                self.close(client, False)
                continue
            if clients[client] != view.session:
                continue
            try:
                self.render(view, now)
            except TmuxError:
                self.tmux.message(client, "agent-overview: preview resource changed; reopen overview")
                self.close(client)
        # A small rotating background budget makes filters useful beyond the page.
        if any(clients.get(v.client) == v.session for v in self.views.values()):
            stale = sorted(self.agents, key=lambda a: self.cache.get(a.pane, (0, ""))[0])
            for agent in stale[:2]:
                cached = self.cache.get(agent.pane)
                if len(self.captures) < 16 and agent.pane not in self.captures and (not cached or now - cached[0] > 2):
                    self.captures[agent.pane] = self.pool.submit(self.tmux.run, "capture-pane", "-p", "-S", "-200", "-t", agent.pane)

    def render(self, view, now):
        search = self.tmux.option("@agent-overview-search", view.session).lower()[:256]
        agents = [a for a in self.agents if search in a.title.lower() or search == a.state]
        width, height, zoom = map(int, self.tmux.display(view.window, "#{window_width} #{window_height} #{window_zoomed_flag}").split())
        if view.panes:
            view.selected = view.sources.get(self.tmux.display(view.window, "#{pane_id}"), view.selected)
        size = capacity(width, height)
        pages = max(1, (len(agents) + size - 1) // size)
        if search != view.search:
            view.page = 0
            view.search = search
        elif view.selected in [a.pane for a in agents] and view.geometry and view.geometry[:2] != (width, height):
            view.page = next(i for i, a in enumerate(agents) if a.pane == view.selected) // size
        view.page = min(view.page, pages - 1)
        visible = agents[view.page * size:(view.page + 1) * size]
        geometry = (width, height, tuple(a.pane for a in visible))
        if geometry != view.geometry:
            if zoom:
                self.tmux.run("resize-pane", "-Z", "-t", view.window)
            desired = max(1, len(visible))
            while len(view.panes) > desired:
                self.tmux.run("kill-pane", "-t", view.panes.pop())
            while len(view.panes) < desired:
                pane = self.tmux.run("split-window", "-d", "-h", "-t", view.panes[0], "-P", "-F", "#{pane_id}", "")
                view.panes.append(pane)
                self.tmux.run("select-layout", "-t", view.window, "tiled")
            self.tmux.run("select-layout", "-t", view.window, native_layout(view.panes, width, height))
            view.sources = dict(zip(view.panes, (a.pane for a in visible)))
            if view.selected in view.sources.values():
                selected = next(p for p, source in view.sources.items() if source == view.selected)
                self.tmux.run("select-pane", "-t", selected)
            view.frames.clear()
            view.geometry = geometry
        for index, pane in enumerate(view.panes):
            pw, ph, active = map(int, self.tmux.display(pane, "#{pane_width} #{pane_height} #{pane_active}").split())
            _, content_height = card_content_size(pw, ph)
            agent = visible[index] if index < len(visible) else None
            if agent:
                captured = self.cache.get(agent.pane)
                if captured and now - captured[0] > 3:
                    captured = None
                if len(self.captures) < 16 and agent.pane not in self.captures and (not captured or now - captured[0] >= .5):
                    self.captures[agent.pane] = self.pool.submit(self.tmux.run, "capture-pane", "-p", "-S", "-200", "-t", agent.pane)
                text = captured[1] if captured else ""
                agent.state = classify(agent.kind, "\n".join(text.splitlines()[-ph:])) if captured else "unknown"
                shared = self.watcher.state(agent, self.agents)
                if shared is not None:
                    agent.state, agent.provenance = shared, "shared"
                else:
                    agent.provenance = "heuristic"
                heading = f"{index + 1} {agent.kind} | {agent.state} ({agent.provenance}) | {agent.session_name}:{agent.index} {agent.pane}"
                offset = view.offsets.get(agent.pane, 0)
                lines = text.splitlines()
                while lines and not lines[-1].strip():
                    lines.pop()
                end = max(0, len(lines) - offset)
                rows = lines[max(0, end - content_height):end]
                if not captured:
                    rows = ["Loading preview..."]
            else:
                heading = "No matching agents"
                rows = ["No running agents detected.", "Use @agent-overview-kind for wrapped processes.", "/ search | Escape clear | q close"]
            frame = card_frame(rows, pw, ph, heading=heading, selected=bool(active),
                               state=agent.state if agent else "unknown")
            if view.frames.get(pane) != (frame, heading):
                self.tmux.run("display-message", "-I", "-t", pane, input=frame)
                view.frames[pane] = (frame, heading)
        hints = f" AGENTS {len(agents)}/{len(self.agents)} | page {view.page + 1}/{pages} | {self.health or self.watcher.health} | Enter focus  z/Z zoom  [] pages  / search  ? help  q close "
        self.tmux.run("set-option", "-t", view.session, "status-left-length", "250")
        self.tmux.run("set-option", "-t", view.session, "status-left", label(hints))

    def shutdown(self):
        for client in list(self.views):
            try:
                self.close(client)
            except TmuxError:
                self.health = "tmux server unavailable during cleanup"
        self.watcher.close()
        self.pool.shutdown(wait=True, cancel_futures=True)


def serve(socket_path):
    directory, path = runtime(socket_path)
    lock_path = directory / "lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if os.path.lexists(path):
            info = os.lstat(path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError("foreign control socket")
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX)
        mask = os.umask(0o077)
        try:
            listener.bind(path)
        finally:
            os.umask(mask)
        listener.listen(16)
        listener.setblocking(False)
        selector = selectors.DefaultSelector()
        selector.register(listener, selectors.EVENT_READ)
        controller = Controller(Tmux(socket_path))
        started, next_tick = time.monotonic(), 0
        try:
            while controller.views or time.monotonic() - started < 5:
                for key, _ in selector.select(.05):
                    connection, _ = key.fileobj.accept()
                    with connection:
                        connection.settimeout(.2)
                        try:
                            data = b""
                            while b"\n" not in data and len(data) <= 4096:
                                chunk = connection.recv(4096)
                                if not chunk:
                                    break
                                data += chunk
                            if len(data) > 4096:
                                raise ValueError("action too large")
                            request = json.loads(data)
                            if not isinstance(request, dict):
                                raise ValueError("invalid action")
                            controller.action(request)
                            response = {"ok": True}
                        except (ValueError, OSError, TmuxError, RuntimeError):
                            response = {"ok": False, "error": "action failed; check target, client, and overview availability"}
                        try:
                            connection.sendall(json.dumps(response).encode() + b"\n")
                        except OSError:
                            controller.health = "action client disconnected"
                if time.monotonic() >= next_tick:
                    controller.tick()
                    next_tick = time.monotonic() + .1
        finally:
            controller.shutdown()
            selector.close()
            listener.close()
            os.unlink(path)
