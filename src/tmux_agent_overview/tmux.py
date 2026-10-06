import subprocess


class TmuxError(RuntimeError):
    pass


class Tmux:
    def __init__(self, socket_path):
        self.socket = socket_path

    def run(self, *args, input=None):
        try:
            result = subprocess.run(
                ["tmux", "-S", self.socket, *args], input=input,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxError("tmux unavailable or command timed out") from exc
        if result.returncode:
            raise TmuxError("tmux command failed: " + args[0])
        return result.stdout.rstrip("\n")

    def option(self, name, target=None):
        return self.run("show-options", *(["-t", target] if target else ["-g"]), "-qv", name)

    def display(self, target, fmt):
        return self.run("display-message", "-p", "-t", target, fmt)

    def client(self, client, fmt):
        for line in self.run("list-clients", "-F", "#{client_name}\t" + fmt).splitlines():
            name, value = line.split("\t", 1)
            if name == client:
                return value
        raise TmuxError("client is no longer attached")

    def message(self, client, text):
        self.run("display-message", "-c", client, "-l", text)
