import argparse
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import time

from .controller import ACTIONS, ENTRY, runtime, serve
from .tmux import Tmux, TmuxError


def install(tmux):
    command = shlex.join([sys.executable, str(ENTRY), "--socket", tmux.socket])
    invoke = command + ' action --client "#{q:client_name}" '
    key = tmux.option("@agent-overview-key") or "O"
    bindings = tmux.run("list-keys", "-T", "prefix").splitlines()
    existing = None
    for line in bindings:
        words = shlex.split(line)
        table_index = words.index("-T")
        if words[table_index + 2] == key:
            existing = line
            break
    if existing and str(ENTRY) not in existing:
        raise RuntimeError("launch key already bound; set @agent-overview-key to a free key")
    tmux.run("bind-key", "-T", "prefix", key, "run-shell", "-b", invoke + "open")
    for key, direction in (("h", "-L"), ("j", "-D"), ("k", "-U"), ("l", "-R"),
                           ("Left", "-L"), ("Down", "-D"), ("Up", "-U"), ("Right", "-R")):
        tmux.run("bind-key", "-T", "agent-overview", key, "select-pane", direction)
    actions = {"Enter": "focus", "z": "zoom", "Z": "zoom-source", "]": "next",
               "[": "previous", "PPage": "scroll-up", "NPage": "scroll-down",
               "g": "live", "q": "close", "Escape": "escape", "?": "help",
               "WheelUpPane": "scroll-up", "WheelDownPane": "scroll-down"}
    for key, action in actions.items():
        tmux.run("bind-key", "-T", "agent-overview", key, "run-shell", "-b", invoke + action)
    for index in range(10):
        tmux.run("bind-key", "-T", "agent-overview", str((index + 1) % 10),
                 "run-shell", "-b", invoke + f"select --index {index}")
    tmux.run("bind-key", "-T", "agent-overview", "MouseDown1Pane", "select-pane", "-t", "=")
    for key, prompt in (("/", "Search"), ("f", "Filter: state or agent kind")):
        tmux.run("bind-key", "-T", "agent-overview", key, "command-prompt", "-p", prompt,
                 'set-option -t "#{session_id}" @agent-overview-search "%%%"')
    tmux.run("bind-key", "-T", "agent-overview", "Any", "display-message", "Read-only preview: Enter to focus an agent")


def send(tmux, request):
    _, path = runtime(tmux.socket)
    for attempt in range(40):
        conn = socket.socket(socket.AF_UNIX)
        conn.settimeout(3)
        try:
            if os.path.lexists(path):
                info = os.lstat(path)
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise RuntimeError("unsafe control socket")
            conn.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            conn.close()
            if attempt == 0:
                tmux.run("run-shell", "-b", shlex.join([sys.executable, str(ENTRY), "--socket", tmux.socket, "serve"]))
            time.sleep(.1)
            continue
        try:
            conn.sendall(json.dumps(request).encode() + b"\n")
            response = b""
            while b"\n" not in response and len(response) < 4096:
                data = conn.recv(4096)
                if not data:
                    break
                response += data
            result = json.loads(response)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "overview action failed"))
            return
        finally:
            conn.close()
    raise RuntimeError("overview controller did not start")


def main():
    parser = argparse.ArgumentParser(description="Native tmux agent overview")
    parser.add_argument("--socket")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    sub.add_parser("serve")
    action = sub.add_parser("action")
    action.add_argument("action", choices=sorted(ACTIONS))
    action.add_argument("--client", required=True)
    action.add_argument("--index", type=int)
    args = parser.parse_args()
    socket_path = args.socket
    if not socket_path:
        result = subprocess.run(["tmux", "display-message", "-p", "#{socket_path}"], capture_output=True, text=True)
        if result.returncode:
            parser.error("run inside tmux or specify --socket")
        socket_path = result.stdout.strip()
    tmux = Tmux(socket_path)
    try:
        if args.command == "install":
            version = tmux.run("-V").split()[-1]
            import re
            match = re.match(r"(\d+)\.(\d+)", version)
            if not match or tuple(map(int, match.groups())) < (3, 2):
                raise RuntimeError("tmux 3.2+ required")
            install(tmux)
        elif args.command == "serve":
            serve(socket_path)
        else:
            send(tmux, {"action": args.action, "client": args.client, "index": args.index})
    except (OSError, ValueError, RuntimeError, TmuxError) as exc:
        message = "agent-overview: " + str(exc)
        if getattr(args, "client", None):
            try:
                tmux.message(args.client, message)
            except TmuxError:
                pass
        print(message, file=sys.stderr)
        sys.exit(1)
