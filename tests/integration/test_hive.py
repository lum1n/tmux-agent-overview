import base64
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import test_native

from tmux_agent_overview.cli import send
from tmux_agent_overview.controller import Controller, runtime

FIXTURE = r'''
package main
import ("bufio"; "encoding/json"; "fmt"; "os")
func main() {
    if len(os.Args) < 3 || os.Args[1] != "agents" { os.Exit(1) }
    switch os.Args[2] {
    case "capabilities":
        fmt.Println(`{"version":1,"commands":["list","capture","attach"],"max_targets":16,"max_lines":500,"max_bytes":65536}`)
    case "list":
        data, err := os.ReadFile(os.Getenv("HIVE_FIXTURE_JSON"))
        if err != nil { os.Exit(1) }
        os.Stdout.Write(data)
        os.Exit(2)
    case "capture":
        rows := []map[string]interface{}{}
        for i:=3; i<len(os.Args)-1; i++ {
            if os.Args[i] == "--id" {
                rows=append(rows,map[string]interface{}{"id":os.Args[i+1],"state":"waiting-permission","provenance":"heuristic","truncated":false,"text":"synthetic-Hive-preview\n>"})
                i++
            }
        }
        json.NewEncoder(os.Stdout).Encode(map[string]interface{}{"version":1,"captures":rows})
    case "attach":
        fmt.Println("synthetic-Hive-attached")
        reader := bufio.NewScanner(os.Stdin)
        for reader.Scan() {
            if reader.Text() == "return" { return }
            fmt.Println("synthetic-input:" + reader.Text())
        }
    default: os.Exit(1)
    }
}
'''


class HiveNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("go"):
            raise unittest.SkipTest("Go is needed to build the synthetic Hive executable")
        cls.fixture_dir = tempfile.TemporaryDirectory(prefix="hive-fixture-", dir=os.environ.get("TMPDIR"))
        source = pathlib.Path(cls.fixture_dir.name) / "main.go"
        source.write_text(FIXTURE)
        cls.binary = str(pathlib.Path(cls.fixture_dir.name) / "hive")
        result = subprocess.run(["go", "build", "-o", cls.binary, str(source)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            cls.fixture_dir.cleanup()
            raise RuntimeError("synthetic Hive build failed")

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.base = test_native.NativeTests()
        self.base.setUp()
        self.tmux = self.base.tmux
        self.base.controller.shutdown()
        self.tmux.run("set-option", "-g", "@agent-overview-hive", "auto")
        self.tmux.run("set-option", "-g", "@agent-overview-hive-bin", self.binary)
        socket = os.path.realpath(self.tmux.socket)
        hosts = []
        for name, server, local in (("local", socket, True), ("box", "/synthetic/tmux.sock", False)):
            ref = {"version": 1, "host": name, "socket": server, "generation": "1:1", "pane": self.base.source}
            opaque = "hive-agent-v1." + base64.urlsafe_b64encode(json.dumps(ref).encode()).decode().rstrip("=")
            row = {"id": opaque, "host": name, "socket": server, "generation": "1:1",
                   "pane": self.base.source, "session": "$0", "window": "@0", "kind": "cursor",
                   "path": "/synthetic/project", "memberships": [
                       {"session": "$0", "window": "@0", "session_name": "fixture", "window_name": "work"}]}
            hosts.append({"host": name, "label": name, "local": local, "status": "online",
                          "servers": [{"socket": server, "generation": "1:1", "status": "online", "agents": [row]}]})
        hosts.append({"host": "offline", "label": "offline", "local": False, "status": "offline",
                      "error": {"code": "offline", "message": "SSH host unavailable"}, "servers": []})
        self.json_path = pathlib.Path(self.base.directory.name) / "hive.json"
        self.json_path.write_text(json.dumps({"version": 1, "hosts": hosts}))
        self.environment = mock.patch.dict(os.environ, {"HIVE_FIXTURE_JSON": str(self.json_path)})
        self.environment.start()
        self.tmux.run("set-environment", "-g", "HIVE_FIXTURE_JSON", str(self.json_path))
        self.base.controller = Controller(self.tmux)
        self.transport_directory = None

    def tearDown(self):
        try:
            if self.transport_directory is not None and self.transport_directory.exists():
                self.wait(lambda: not (self.transport_directory / "control.sock").exists())
                (self.transport_directory / "lock").unlink()
                self.transport_directory.rmdir()
        finally:
            try:
                self.base.tearDown()
            finally:
                self.environment.stop()

    def wait(self, predicate, tick=False):
        for _ in range(200):
            if tick:
                self.base.controller.tick()
            if predicate():
                return
            time.sleep(.03)
        self.fail("synthetic Hive operation did not complete")

    def test_grid_deduplication_previews_zoom_and_search(self):
        view = self.base.open()
        controller = self.base.controller
        identity = self.tmux.display(self.base.source, "#{window_id} #{session_id} #{pane_pid}")
        self.wait(lambda: len(controller.agents) == 2 and any(key.startswith("hive:") for key in controller.cache), True)
        self.assertEqual(len(view.sources), 2)
        self.assertEqual(len({agent.key for agent in controller.agents}), 2)
        remote = next(agent for agent in controller.agents if agent.hive_id)
        pane = next(pane for pane, source in view.sources.items() if source == remote.key)
        controller.render(view, time.monotonic())
        preview = self.tmux.run("capture-pane", "-p", "-t", pane)
        self.assertIn("box/cursor", preview)
        self.assertIn("synthetic-Hive-preview", preview)
        self.assertIn("1 unavailable", controller.hive_health)
        self.assertEqual(remote.state, "waiting-permission")
        controller.hive_states[remote.key] = (time.monotonic(), "unknown", "heuristic")
        controller.render(view, time.monotonic())
        unknown = self.tmux.run("capture-pane", "-p", "-t", pane)
        self.assertIn("unknown", unknown)
        self.assertNotIn("Hive?", unknown)
        controller.hive_states[remote.key] = (time.monotonic(), "thinking", "shared")
        controller.render(view, time.monotonic())
        shared = self.tmux.run("capture-pane", "-p", "-t", pane)
        self.assertIn("thinking (Hive)", shared)
        self.tmux.run("select-pane", "-t", pane)
        self.base.action("zoom")
        controller.render(view, time.monotonic())
        self.assertEqual(self.tmux.display(view.window, "#{window_zoomed_flag}"), "1")
        self.assertEqual(self.tmux.display(self.base.source, "#{window_zoomed_flag}"), "0")
        self.base.action("zoom-source")
        self.assertEqual(self.tmux.client(self.base.client, "#{session_id}"), view.session)
        self.tmux.run("set-option", "-t", view.session, "@agent-overview-search", "box")
        controller.render(view, time.monotonic())
        self.assertEqual(list(view.sources.values()), [remote.key])
        self.assertEqual(self.tmux.display(self.base.source, "#{window_id} #{session_id} #{pane_pid}"), identity)
        self.base.action("close")

    def test_transport_focus_uses_normal_input_and_returns_to_grid(self):
        send(self.tmux, {"action": "open", "client": self.base.client})
        self.transport_directory = pathlib.Path(runtime(self.tmux.socket)[1]).parent
        session = self.tmux.client(self.base.client, "#{session_id}")
        def remote_pane():
            rows = self.tmux.run("list-panes", "-t", session, "-F", "#{pane_id}").splitlines()
            return next((pane for pane in rows if "synthetic-Hive-preview" in self.tmux.run("capture-pane", "-p", "-t", pane)), "")
        self.wait(remote_pane)
        pane = remote_pane()
        self.tmux.run("select-pane", "-t", pane)
        send(self.tmux, {"action": "focus", "client": self.base.client})
        attached = self.tmux.client(self.base.client, "#{session_id}")
        self.assertNotEqual(attached, session)
        self.assertEqual(self.tmux.option("key-table", attached), "root")
        target = self.tmux.client(self.base.client, "#{pane_id}")
        self.wait(lambda: "synthetic-Hive-attached" in self.tmux.run("capture-pane", "-p", "-t", target))
        self.tmux.run("send-keys", "-t", target, "-l", "q")
        self.tmux.run("send-keys", "-t", target, "Enter")
        self.wait(lambda: "synthetic-input:q" in self.tmux.run("capture-pane", "-p", "-t", target))
        self.tmux.run("send-keys", "-t", target, "-l", "return")
        self.tmux.run("send-keys", "-t", target, "Enter")
        self.wait(lambda: self.tmux.client(self.base.client, "#{session_id}") == session)
        self.wait(lambda: attached not in self.tmux.run("list-sessions", "-F", "#{session_id}").splitlines())
        self.tmux.run("select-pane", "-t", remote_pane())
        send(self.tmux, {"action": "focus", "client": self.base.client})
        attached = self.tmux.client(self.base.client, "#{session_id}")
        target = self.tmux.client(self.base.client, "#{pane_id}")
        self.wait(lambda: "synthetic-Hive-attached" in self.tmux.run("capture-pane", "-p", "-t", target))
        send(self.tmux, {"action": "open", "client": self.base.client})
        self.assertEqual(self.tmux.client(self.base.client, "#{session_id}"), session)
        send(self.tmux, {"action": "close", "client": self.base.client})
        self.assertNotIn(attached, self.tmux.run("list-sessions", "-F", "#{session_id}").splitlines())
        _, path = runtime(self.tmux.socket)
        self.wait(lambda: not os.path.exists(path))

    def test_owner_disconnect_cleans_grid_and_attachment(self):
        send(self.tmux, {"action": "open", "client": self.base.client})
        self.transport_directory = pathlib.Path(runtime(self.tmux.socket)[1]).parent
        session = self.tmux.client(self.base.client, "#{session_id}")
        def remote_pane():
            rows = self.tmux.run("list-panes", "-t", session, "-F", "#{pane_id}").splitlines()
            return next((pane for pane in rows if "synthetic-Hive-preview" in self.tmux.run("capture-pane", "-p", "-t", pane)), "")
        self.wait(remote_pane)
        self.tmux.run("select-pane", "-t", remote_pane())
        send(self.tmux, {"action": "focus", "client": self.base.client})
        attached = self.tmux.client(self.base.client, "#{session_id}")
        self.base.client_process.terminate()
        self.base.client_process.wait(timeout=3)
        # The attachment can end on its own when its client goes away, before
        # the controller's next tick removes the grid; wait for both.
        self.wait(lambda: not {attached, session} & set(self.tmux.run("list-sessions", "-F", "#{session_id}").splitlines()))
        self.assertEqual(self.tmux.display(self.base.source, "#{pane_dead}"), "0")
        self.wait(lambda: not (self.transport_directory / "control.sock").exists())


if __name__ == "__main__":
    unittest.main()
