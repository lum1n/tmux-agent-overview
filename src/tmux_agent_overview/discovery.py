import os
import re
import subprocess

from .model import Agent, KINDS


def detect(command):
    base = os.path.basename(command).lower()
    direct = {"copilot": "copilot", "claude": "claude", "codex": "codex",
              "pi": "pi", "opencode": "opencode", "cursor-agent": "cursor"}
    if base in direct:
        return direct[base]
    patterns = (("copilot", "@github/copilot/"), ("claude", "@anthropic-ai/claude-code/"),
                ("codex", "@openai/codex/"), ("pi", "pi-coding-agent/"),
                ("opencode", "opencode-ai/"), ("cursor", "/cursor-agent/"))
    for kind, path in patterns:
        if path in command:
            return kind
    return None


def process_tree():
    result = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=2)
    if result.returncode:
        raise RuntimeError("process discovery unavailable")
    children, names = {}, {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            pid, parent = int(fields[0]), int(fields[1])
            children.setdefault(parent, []).append(pid)
            names[pid] = fields[2]
    return children, names


def descendant_kind(root, children, names):
    pending, seen, found = [root], set(), set()
    while pending and len(seen) < 1024:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        command = names.get(pid, "")
        kind = detect(command)
        if not kind and os.path.basename(command) in ("node", "bun", "deno", "MainThread"):
            result = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=1)
            if result.returncode == 0:
                kind = detect(result.stdout[:8192])
        if kind:
            found.add(kind)
        pending.extend(children.get(pid, []))
    return next(iter(found)) if len(found) == 1 else None


def inventory(tmux):
    fmt = "\t".join(("#{pane_id}", "#{window_id}", "#{session_id}", "#{session_name}",
                     "#{window_index}", "#{pane_current_command}", "#{pane_pid}", "#{pane_active}",
                     "#{pane_current_path}", "#{@agent-overview-owned}", "#{@agent-overview-kind}", "#{pane_dead}",
                     "#{window_name}"))
    output = tmux.run("list-panes", "-a", "-F", fmt)
    children, names = process_tree()
    agents = {}
    for line in output.splitlines():
        fields = line.split("\t", 12)
        if len(fields) != 13:
            continue
        pane, window, session, name, index, command, pid, active, path, owned, override, dead, window_name = fields
        if owned or dead == "1" or override == "off" or not re.fullmatch(r"%\d+", pane) or not pid.isdigit():
            continue
        kind = override if override in KINDS else (detect(command) or descendant_kind(int(pid), children, names))
        if kind and pane not in agents:
            agents[pane] = Agent(pane, window, session, name, index, command, int(pid), active == "1", path, kind,
                                 window_name=window_name)
    return sorted(agents.values(), key=lambda a: (a.session_name, int(a.index), int(a.pane[1:])))
