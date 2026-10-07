import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from tmux_agent_overview.cli import install, send
from tmux_agent_overview.controller import Controller, runtime
from tmux_agent_overview.discovery import inventory
from tmux_agent_overview.model import native_layout
from tmux_agent_overview.tmux import Tmux


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="overview-test-", dir=os.environ.get("TMPDIR"))
        self.tmux = Tmux(self.directory.name + "/tmux.sock")
        self.tmux.run("-f", "/dev/null", "new-session", "-d", "-s", "fixture", "-x", "160", "-y", "48", "cat")
        self.tmux.run("set-option", "-g", "@agent-overview-hive", "off")
        self.source = self.tmux.display("fixture", "#{pane_id}")
        self.tmux.run("set-option", "-p", "-t", self.source, "@agent-overview-kind", "claude")
        self.client_process = subprocess.Popen(
            ["tmux", "-S", self.tmux.socket, "-C", "attach-session", "-t", "fixture"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            clients = self.tmux.run("list-clients", "-F", "#{client_name}").splitlines()
            if clients:
                break
            time.sleep(.02)
        self.client = clients[0]
        install(self.tmux)
        self.controller = Controller(self.tmux)

    def tearDown(self):
        self.controller.shutdown()
        self.client_process.terminate()
        self.client_process.wait(timeout=3)
        self.client_process.stdin.close()
        self.tmux.run("kill-server")
        self.directory.cleanup()

    def open(self):
        self.controller.agents = inventory(self.tmux)
        self.controller.open(self.client)
        view = self.controller.views[self.client]
        self.controller.render(view, time.monotonic())
        return view

    def prefix_binding(self, key):
        # tmux 3.7c prints nothing for `list-keys -T table key`; scan the table.
        for line in self.tmux.run("list-keys", "-T", "prefix").splitlines():
            words = shlex.split(line)
            if words[words.index("-T") + 2] == key:
                return line
        return ""

    def action(self, name):
        self.controller.action({"action": name, "client": self.client})

    def test_empty_pane_and_layout(self):
        pane = self.tmux.run("split-window", "-d", "-h", "-t", self.source, "-P", "-F", "#{pane_id}", "")
        self.tmux.run("display-message", "-I", "-t", pane, input="synthetic preview")
        self.assertIn("synthetic preview", self.tmux.run("capture-pane", "-p", "-t", pane))
        width, height = map(int, self.tmux.display(self.source, "#{window_width} #{window_height}").split())
        self.tmux.run("select-layout", "-t", self.source, native_layout([self.source, pane], width, height))

    def test_preview_preserves_source_and_focus(self):
        identity = self.tmux.display(self.source, "#{window_id} #{session_id} #{pane_pid}")
        view = self.open()
        self.assertEqual(self.tmux.option("key-table", view.session), "agent-overview")
        self.action("zoom")
        self.assertEqual(self.tmux.display(self.source, "#{window_zoomed_flag}"), "0")
        self.action("focus")
        self.assertEqual(self.tmux.client(self.client, "#{pane_id}"), self.source)
        self.assertEqual(self.tmux.display(self.source, "#{window_id} #{session_id} #{pane_pid}"), identity)
        self.action("open")
        self.action("close")
        self.assertFalse(self.controller.views)

    def test_source_zoom_restores_and_preserves_existing(self):
        self.tmux.run("split-window", "-d", "-h", "-t", self.source, "cat")
        self.open()
        self.action("zoom-source")
        self.assertEqual(self.tmux.display(self.source, "#{window_zoomed_flag}"), "1")
        self.action("open")
        self.assertEqual(self.tmux.display(self.source, "#{window_zoomed_flag}"), "0")
        self.tmux.run("resize-pane", "-Z", "-t", self.source)
        self.action("zoom-source")
        self.action("open")
        self.assertEqual(self.tmux.display(self.source, "#{window_zoomed_flag}"), "1")

    def test_manual_zoom_cancels_restore(self):
        self.tmux.run("split-window", "-d", "-h", "-t", self.source, "cat")
        self.open()
        self.action("zoom-source")
        self.tmux.run("resize-pane", "-Z", "-t", self.source)
        self.tmux.run("resize-pane", "-Z", "-t", self.source)
        self.action("open")
        self.assertEqual(self.tmux.display(self.source, "#{window_zoomed_flag}"), "1")

    def test_multiple_agents_linked_and_exclusion(self):
        pane = self.tmux.run("split-window", "-d", "-h", "-t", self.source, "-P", "-F", "#{pane_id}", "cat")
        self.tmux.run("set-option", "-p", "-t", pane, "@agent-overview-kind", "codex")
        self.tmux.run("new-session", "-d", "-s", "linked", "cat")
        self.tmux.run("link-window", "-s", "fixture:0", "-t", "linked:1")
        self.assertEqual(len(inventory(self.tmux)), 2)
        view = self.open()
        self.assertEqual(len(view.sources), 2)
        self.assertEqual(len(inventory(self.tmux)), 2)
        self.tmux.run("set-option", "-p", "-t", pane, "@agent-overview-kind", "off")
        self.assertEqual(len(inventory(self.tmux)), 1)

    def test_install_preserves_status_and_chooser(self):
        self.tmux.run("set-option", "-g", "status-right", "synthetic-state")
        self.tmux.run("bind-key", "s", "display-message", "synthetic-chooser")
        install(self.tmux)
        install(self.tmux)
        self.assertEqual(self.tmux.option("status-right"), "synthetic-state")
        self.assertIn("synthetic-chooser", self.prefix_binding("s"))
        # Another checkout's launch binding is replaced, not treated as foreign.
        self.tmux.run("bind-key", "O", "run-shell", "-b", '/usr/bin/python3 /old/checkout/scripts/overview.py '
                      '--socket /old.sock action --client "#{q:client_name}" open')
        install(self.tmux)
        self.assertNotIn("/old/checkout/", self.prefix_binding("O"))
        self.assertIn("overview.py", self.prefix_binding("O"))
        self.tmux.run("bind-key", "O", "display-message", "existing")
        with self.assertRaises(RuntimeError):
            install(self.tmux)

    def test_plugin_entrypoint_loads_on_current_server(self):
        entrypoint = pathlib.Path(__file__).resolve().parents[2] / "tmux-agent-overview.tmux"
        environment = dict(os.environ, TMUX=f"{self.tmux.socket},0,0")
        result = subprocess.run(
            [str(entrypoint)], env=environment,
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("overview.py", self.prefix_binding("O"))

    def test_controller_transport_and_cleanup(self):
        send(self.tmux, {"action": "open", "client": self.client})
        _, path = runtime(self.tmux.socket)
        self.assertTrue(os.path.exists(path))
        send(self.tmux, {"action": "close", "client": self.client})
        for _ in range(100):
            if not os.path.exists(path):
                break
            time.sleep(.05)
        self.assertFalse(os.path.exists(path))
        directory = pathlib.Path(path).parent
        (directory / "lock").unlink()
        directory.rmdir()

    def test_quota_status_and_limited_card(self):
        reset = time.time() + 3 * 3600 + 60
        self.controller.watcher.quota = {
            "claude": ("Max 5x", (("5h", 100.0, reset), ("7d", 40.0, None)), False),
            "codex": ("plus", (("5h", 10.0, None),), False),
        }
        view = self.open()
        status = self.tmux.option("status-right", view.session)
        # The control-mode client is 80 columns: status-left shrinks so quota fits.
        self.assertIn("claude · Max 5x · 5h full · 3h", status)
        self.assertIn("#[fg=colour203]", status)
        self.assertEqual(self.tmux.option("status-left", view.session), " AGENTS 1/1 | ? help ")
        # Kinds without a local agent stay out of the status line.
        self.assertNotIn("codex", status)
        self.assertIn("| limit 3h ", self.tmux.run("capture-pane", "-p", "-t", view.panes[0]))
        # A Hive card carries its own host's reading; it never borrows local quota.
        from dataclasses import replace
        remote = replace(self.controller.agents[0], pane="%900", hive_id="hive-agent-v1.fixture", host="box",
                         host_label="mac", kind="claude", quota=("Pro", (("5h", 100.0, reset),), False))
        stale_twin = replace(remote, pane="%901", socket="/other", quota=("Pro", (("5h", 1.0, None),), True))
        self.controller.agents += [stale_twin, remote]
        self.controller.render(view, time.monotonic())
        status = self.tmux.option("status-right", view.session)
        # 80 columns fit one compact meter; the other is counted, never cut mid-text.
        self.assertIn("]claude · 5h full · 3h#", status)
        self.assertIn("]+1#", status)
        frames = [self.tmux.run("capture-pane", "-p", "-t", pane) for pane in view.panes]
        self.assertTrue(any("mac/claude" in frame and "| limit 3h " in frame for frame in frames))

    def test_watcher_socket_shared_copilot(self):
        import json
        import socket
        from tmux_agent_overview.watcher import Watcher
        path = self.directory.name + "/watcher.sock"
        listener = socket.socket(socket.AF_UNIX)
        mask = os.umask(0o077)
        try:
            listener.bind(path)
        finally:
            os.umask(mask)
        listener.listen(1)
        watcher = Watcher()
        try:
            watcher.poll(path)
            connection, _ = listener.accept()
            with connection:
                event = {"v": 1, "type": "snapshot", "agents": [
                    {"session": "fixture", "window": 0, "kind": "copilot", "state": "waiting-permission"}]}
                connection.sendall(json.dumps(event).encode() + b"\n")
                watcher.poll(path)
                self.assertEqual(watcher.records[("fixture", 0)], ("copilot", "waiting-permission"))
                # Quota and future event types arrive on the same stream without a reconnect.
                for event in ({"v": 1, "type": "quota", "kind": "claude", "plan": "Max 5x", "stale": False,
                               "windows": [{"id": "session", "label": "5h", "usedPercent": 42}]},
                              {"v": 1, "type": "future-event"}):
                    connection.sendall(json.dumps(event).encode() + b"\n")
                watcher.poll(path)
                self.assertEqual(watcher.health, "shared watcher")
                self.assertEqual(watcher.quota["claude"], ("Max 5x", (("5h", 42.0, None),), False))
                self.assertIn(("fixture", 0), watcher.records)
            watcher.poll(path)
            self.assertFalse(watcher.records)
            self.assertFalse(watcher.quota)
            self.assertEqual(watcher.health, "watcher unavailable")
        finally:
            watcher.close()
            listener.close()
            os.unlink(path)

    def test_pagination_search_and_navigation_latency(self):
        for index in range(24):
            pane = self.tmux.run("new-window", "-d", "-t", "fixture", "-P", "-F", "#{pane_id}", "cat")
            self.tmux.run("set-option", "-p", "-t", pane, "@agent-overview-kind", "copilot")
        self.tmux.run("refresh-client", "-t", self.client, "-C", "160,48")
        view = self.open()
        self.assertEqual(len(self.controller.agents), 25)
        self.assertEqual(len(view.sources), 12)
        first = set(view.sources.values())
        self.action("next")
        self.controller.render(view, time.monotonic())
        self.assertFalse(first.intersection(view.sources.values()))
        timings = []
        for _ in range(20):
            started = time.monotonic()
            self.tmux.run("select-pane", "-R", "-t", view.window)
            timings.append(time.monotonic() - started)
        self.assertLess(sorted(timings)[18], .15)
        self.tmux.run("set-option", "-t", view.session, "@agent-overview-search", "claude")
        self.controller.render(view, time.monotonic())
        self.assertEqual(list(view.sources.values()), [self.source])
        self.assertEqual(view.page, 0)

    def test_shutdown_after_manually_closed_view(self):
        view = self.open()
        self.tmux.run("switch-client", "-c", self.client, "-t", "fixture")
        self.tmux.run("kill-session", "-t", view.session)
        self.controller.close(self.client)
        self.assertFalse(self.controller.views)

    def test_preview_renders_recent_output(self):
        self.tmux.run("send-keys", "-t", self.source, "-l", "synthetic-agent-output")
        self.tmux.run("send-keys", "-t", self.source, "Enter")
        time.sleep(.05)
        view = self.open()
        self.controller.cache[self.source] = (time.monotonic(), self.tmux.run("capture-pane", "-p", "-t", self.source))
        self.controller.render(view, time.monotonic())
        self.assertIn("synthetic-agent-output", self.tmux.run("capture-pane", "-p", "-t", view.panes[0]))

    def test_card_gutters_survive_zoom_and_scrolling(self):
        source = self.tmux.run("split-window", "-d", "-h", "-t", self.source, "-P", "-F", "#{pane_id}", "cat")
        self.tmux.run("set-option", "-p", "-t", source, "@agent-overview-kind", "copilot")
        view = self.open()
        self.assertEqual(self.tmux.option("pane-border-status", view.window), "off")
        self.assertEqual(self.tmux.option("pane-border-style", view.window), "fg=colour234,bg=colour234")
        self.assertEqual(self.tmux.option("pane-active-border-style", view.window), "fg=colour234,bg=colour234")
        self.controller.cache[self.source] = (
            time.monotonic(), "\n".join(f"row-{index:03d} " + "x" * 200 for index in range(100)))
        for action in (None, "zoom", "scroll-up", "zoom"):
            if action:
                self.action(action)
            self.controller.render(view, time.monotonic())
            pane = view.panes[0]
            width, height = map(int, self.tmux.display(pane, "#{pane_width} #{pane_height}").split())
            captured = subprocess.run(
                ["tmux", "-S", self.tmux.socket, "capture-pane", "-p", "-t", pane],
                capture_output=True, text=True, timeout=2, check=True)
            rows = captured.stdout.splitlines()
            self.assertFalse(rows[0].strip())
            self.assertTrue(rows[1].startswith("  ╭"))
            self.assertTrue(rows[1].endswith("╮"))
            self.assertIn("1 claude", rows[1])
            self.assertTrue(rows[2].startswith("  │row-"))
            self.assertTrue(all(row.startswith("  │") and row.endswith("│") for row in rows[2:-2]))
            self.assertTrue(all(len(row.rstrip()) <= width - 2 for row in rows))
            self.assertTrue(rows[-2].startswith("  ╰"))
            self.assertTrue(rows[-2].endswith("╯"))
            self.assertFalse(rows[-1].strip())
            self.assertEqual(len(rows), height)
        selected = "\x1b[38;5;81;48;5;234m"
        normal = "\x1b[38;5;238;48;5;234m"
        self.assertIn(selected, view.frames[view.panes[0]][0])
        self.assertIn(normal, view.frames[view.panes[1]][0])
        self.tmux.run("select-pane", "-t", view.panes[1])
        self.controller.render(view, time.monotonic())
        self.assertIn(normal, view.frames[view.panes[0]][0])
        self.assertIn(selected, view.frames[view.panes[1]][0])

    def test_rounded_cards_resize_and_keep_gutters_outside(self):
        view = self.open()
        for width, height in ((80, 24), (10, 6), (160, 48)):
            self.tmux.run("refresh-client", "-t", self.client, "-C", f"{width},{height}")
            self.controller.cache[self.source] = (time.monotonic(), "x" * 200)
            self.controller.render(view, time.monotonic())
            pane = view.panes[0]
            pw, ph = map(int, self.tmux.display(pane, "#{pane_width} #{pane_height}").split())
            captured = subprocess.run(
                ["tmux", "-S", self.tmux.socket, "capture-pane", "-p", "-t", pane],
                capture_output=True, text=True, timeout=2, check=True)
            rows = captured.stdout.splitlines()
            with self.subTest(width=width, height=height):
                self.assertEqual(len(rows), ph)
                self.assertFalse(rows[0].strip())
                self.assertTrue(rows[1].startswith("  ╭"))
                self.assertEqual(rows[2], "  │" + "x" * (pw - 6) + "│")
                self.assertTrue(rows[-2].startswith("  ╰ fix") and rows[-2].endswith("╯"))
                self.assertEqual(len(rows[-2]), pw - 2)
                self.assertFalse(rows[-1].strip())
                styled = self.tmux.run("capture-pane", "-p", "-e", "-t", pane)
                self.assertIn("\x1b[48;5;234m", styled)
                self.assertIn("\x1b[48;5;235m", styled)


if __name__ == "__main__":
    unittest.main()
