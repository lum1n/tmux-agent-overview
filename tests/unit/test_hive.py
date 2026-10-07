import copy
import base64
import json
import pathlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from dataclasses import replace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from tmux_agent_overview.hive import Hive, HiveError, bounded_json, observation_age, parse_captures, parse_inventory
from tmux_agent_overview.controller import Controller


def host_record(host="box", socket="/fixture/tmux.sock", local=False):
    ref = {"version": 1, "host": host, "socket": socket, "generation": "1:1", "pane": "%0"}
    opaque = "hive-agent-v1." + base64.urlsafe_b64encode(json.dumps(ref).encode()).decode().rstrip("=")
    agent = {"id": opaque, "host": host, "socket": socket,
             "generation": "1:1", "pane": "%0", "window": "@0", "session": "$0",
             "kind": "cursor", "path": "/synthetic/project", "memberships": [
                 {"session": "$0", "window": "@0", "session_name": "fixture", "window_name": "work"}]}
    return {"host": host, "label": host, "local": local, "status": "online",
            "servers": [{"socket": socket, "generation": "1:1", "status": "online", "agents": [agent]}]}


def response(*hosts):
    return {"version": 1, "hosts": list(hosts)}


def capture_record(agent, **fields):
    row = {"id": agent.hive_id, "state": "idle", "provenance": "heuristic",
           "truncated": False, "text": "synthetic\n>"}
    row.update(fields)
    return row


class HiveTests(unittest.TestCase):
    def test_observation_timestamps(self):
        with mock.patch("tmux_agent_overview.hive.time.time", return_value=10):
            self.assertEqual(observation_age(None), 0)
            self.assertEqual(observation_age("1970-01-01T00:00:05Z"), 5)
            self.assertEqual(observation_age("1970-01-01T01:00:05+01:00"), 5)
            self.assertEqual(observation_age("1970-01-01T00:00:20Z"), 0)
        for invalid in ("invalid", "1970-01-01T00:00:05", "x" * 65, 10, True, []):
            with self.subTest(timestamp=invalid), self.assertRaises(HiveError):
                observation_age(invalid)

    def test_optional_detection_and_capabilities(self):
        tmux = mock.Mock()
        tmux.option.side_effect = lambda name: {"@agent-overview-hive": "off"}.get(name, "")
        with mock.patch("tmux_agent_overview.hive.shutil.which") as which:
            self.assertIsNone(Hive.detect(tmux))
            which.assert_not_called()
        tmux.option.return_value = ""
        tmux.option.side_effect = None
        with mock.patch("tmux_agent_overview.hive.shutil.which", return_value=None):
            self.assertIsNone(Hive.detect(tmux))
            tmux.option.side_effect = lambda name: "on" if name == "@agent-overview-hive" else ""
            with self.assertRaisesRegex(HiveError, "executable unavailable"):
                Hive.detect(tmux)
        hive = Hive("/fixture/hive")
        caps = {"version": 1, "commands": ["list", "capture", "attach"],
                "max_targets": 16, "max_lines": 500, "max_bytes": 65536}
        with mock.patch("tmux_agent_overview.hive.bounded_json", return_value=caps) as run:
            hive.check()
            hive.check()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(hive.max_lines, 200)
        caps["commands"] = ["list"]
        with mock.patch("tmux_agent_overview.hive.bounded_json", return_value=caps):
            with self.assertRaises(HiveError):
                Hive("/fixture/hive").check()

    def test_identity_and_local_deduplication(self):
        current = "/fixture/current.sock"
        first = host_record(socket=current)
        other = host_record("other", current)
        local = host_record("local", current, True)
        local_other = host_record("local", "/fixture/other.sock", True)
        local["servers"].extend(local_other["servers"])
        agents, health = parse_inventory(response(local, first, other), current)
        self.assertEqual(len(agents), 3)
        self.assertEqual(len({agent.key for agent in agents}), 3)
        self.assertEqual({agent.pane for agent in agents}, {"%0"})
        self.assertEqual(health, "Hive connected")
        duplicate = copy.deepcopy(first["servers"][0]["agents"][0])
        duplicate["id"] += "A"
        first["servers"][0]["agents"].append(duplicate)
        parsed, _ = parse_inventory(response(first), current)
        self.assertEqual(len(parsed), 1)
        self.assertIn("box", parsed[0].title)

    def test_unavailable_is_not_a_healthy_empty_inventory(self):
        failed = {"host": "offline", "label": "offline", "local": False, "status": "offline",
                  "error": {"code": "offline", "message": "SSH host unavailable"}, "servers": []}
        agents, health = parse_inventory(response(host_record(), failed), "/fixture/current.sock")
        self.assertEqual(len(agents), 1)
        self.assertIn("1 unavailable", health)
        degraded = host_record()
        degraded["status"] = "degraded"
        degraded["error"] = {"code": "discovery", "message": "discovery incomplete"}
        _, health = parse_inventory(response(degraded), "/fixture/current.sock")
        self.assertIn("1 unavailable", health)
        bad = host_record()
        bad["servers"][0]["status"] = "unavailable"
        bad["servers"][0]["error"] = {"code": "unavailable", "message": "tmux unavailable"}
        agents, health = parse_inventory(response(bad), "/fixture/current.sock")
        self.assertFalse(agents)
        self.assertIn("1 unavailable", health)

    def test_inventory_rejects_invalid_fields(self):
        for field, invalid in (("pane", "-bad"), ("id", "not-a-reference"), ("kind", "shell"),
                               ("host", "foreign"), ("path", "x" * 16385), ("memberships", [])):
            host = host_record()
            host["servers"][0]["agents"][0][field] = invalid
            with self.subTest(field=field), self.assertRaises(HiveError):
                parse_inventory(response(host), "/fixture/current.sock")
        for invalid in ({"version": True, "hosts": []}, {"version": 2, "hosts": []},
                        {"version": 1, "hosts": {}}, response(*[host_record()] * 65)):
            with self.assertRaises(HiveError):
                parse_inventory(invalid, "/fixture/current.sock")
        first, other = host_record(), host_record("other")
        other["servers"][0]["agents"][0]["id"] = first["servers"][0]["agents"][0]["id"]
        with self.assertRaises(HiveError):
            parse_inventory(response(first, other), "/fixture/current.sock")

    def test_capture_bounds_sanitization_and_failures(self):
        agents, _ = parse_inventory(response(host_record()), "/fixture/current.sock")
        agent = agents[0]
        row = capture_record(agent, text="\x1b]52;c;unsafe\x07synthetic\n>")
        parsed = parse_captures({"version": 1, "captures": [row]}, agents)
        self.assertEqual(parsed[agent.key], ("synthetic\n>", "", "idle", "heuristic", "", 0))
        row.pop("text")
        self.assertEqual(parse_captures({"version": 1, "captures": [row]}, agents)[agent.key], ("", "", "idle", "heuristic", "", 0))
        row = capture_record(agent, state="unknown", provenance="unavailable", text="",
                             error={"code": "stale", "message": "rediscover agents"})
        self.assertEqual(parse_captures({"version": 1, "captures": [row]}, agents)[agent.key], (None, "stale", "unknown", "unavailable", "", 0))
        for rows in ([], [capture_record(agent)] * 2,
                     [capture_record(agent, id="hive-agent-v1.Zm9yZWlnbg")],
                     [capture_record(agent, text="x" * 65537)],
                     [capture_record(agent, state="invalid")]):
            with self.assertRaises(HiveError):
                parse_captures({"version": 1, "captures": rows}, agents)

    def test_visible_capture_is_one_bounded_batch(self):
        agents, _ = parse_inventory(response(host_record(), host_record("other")), "/fixture/current.sock")
        agents[1].hive_id += "A"
        hive = Hive("/fixture/hive")
        payload = {"version": 1, "captures": [capture_record(agent) for agent in agents]}
        with mock.patch("tmux_agent_overview.hive.bounded_json", return_value=payload) as run:
            hive.capture(agents)
        command = run.call_args.args[0]
        self.assertEqual(command.count("--id"), 2)
        self.assertEqual(command[:3], ["/fixture/hive", "agents", "capture"])
        self.assertIn("--json", command)
        self.assertNotIn("ssh", command)
        for invalid in ([], agents * 9, [agents[0]] * 2):
            with self.assertRaises(HiveError):
                hive.capture(invalid)

    def test_watcher_state_fallback_and_observation_age(self):
        host = host_record()
        row = host["servers"][0]["agents"][0]
        row.update(state="thinking", provenance="shared", observed_at="1970-01-01T00:00:00Z")
        with mock.patch("tmux_agent_overview.hive.time.time", return_value=20):
            agents, _ = parse_inventory(response(host), "/fixture/current.sock")
        self.assertEqual((agents[0].state, agents[0].provenance), ("unknown", "unavailable"))
        agent = agents[0]
        payload = capture_record(agent, state="thinking", provenance="shared", time="1970-01-01T00:00:00Z")
        with mock.patch("tmux_agent_overview.hive.time.time", return_value=10):
            parsed = parse_captures({"version": 1, "captures": [payload]}, agents)[agent.key]
        self.assertEqual(parsed, ("synthetic\n>", "", "thinking", "shared", "", 10))
        payload.update(provenance="heuristic", state_error={"code": "timeout", "message": "optional watcher unavailable"})
        with mock.patch("tmux_agent_overview.hive.time.time", return_value=10):
            parsed = parse_captures({"version": 1, "captures": [payload]}, agents)[agent.key]
        self.assertEqual(parsed[0:2], ("synthetic\n>", ""))
        self.assertEqual(parsed[4], "timeout")
        payload["time"] = "invalid"
        with self.assertRaises(HiveError):
            parse_captures({"version": 1, "captures": [payload]}, agents)

    def test_bounded_subprocess_and_content_free_errors(self):
        command = [sys.executable, "-c", 'print(\'{"version":1,"hosts":[]}\')']
        self.assertEqual(bounded_json(command, 2)["hosts"], [])
        partial = command[:2] + [command[2] + "; import sys; sys.exit(2)"]
        self.assertEqual(bounded_json(partial, 2, True)["version"], 1)
        with self.assertRaises(HiveError):
            bounded_json(partial, 2)
        for program in ("import time; time.sleep(5)",
                        "import sys; sys.stdout.write('x' * (9 * 1024 * 1024))",
                        "import sys; sys.stderr.write('sensitive-fixture'); sys.exit(1)"):
            with self.assertRaises(HiveError) as caught:
                bounded_json([sys.executable, "-c", program], .2)
            self.assertNotIn("sensitive-fixture", str(caught.exception))

    def test_shutdown_cancels_running_command(self):
        event = threading.Event()
        timer = threading.Timer(.1, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(HiveError, "cancelled"):
                bounded_json([sys.executable, "-c", "import time; time.sleep(10)"], 15, cancel=event)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            timer.join()

    def test_timeout_cleans_descendants_after_parent_exits(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            path = pathlib.Path(directory) / "child.pid"
            program = ("import subprocess, sys, pathlib; "
                       "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)']); "
                       f"pathlib.Path({str(path)!r}).write_text(str(child.pid))")
            with self.assertRaises(HiveError):
                bounded_json([sys.executable, "-c", program], .5)
            child = int(path.read_text())
            for _ in range(50):
                result = subprocess.run(["ps", "-p", str(child), "-o", "stat="],
                                        capture_output=True, text=True, timeout=1)
                if not result.stdout.strip() or result.stdout.strip().startswith("Z"):
                    break
                time.sleep(.02)
            else:
                os.kill(child, 15)
                self.fail("synthetic descendant survived its operation timeout")


class HiveSchedulingTests(unittest.TestCase):
    def setUp(self):
        tmux = mock.Mock(socket="/fixture/current.sock")
        tmux.option.return_value = "off"
        self.controller = Controller(tmux)
        self.controller.hive_pool.shutdown()
        self.controller.hive_pool = mock.Mock()
        self.controller.hive_pool.submit.return_value = Future()
        self.controller.hive = mock.Mock(max_targets=16)
        self.controller.next_hive_list = 1000
        base = parse_inventory(response(host_record()), tmux.socket)[0][0]
        self.agents = [replace(base, pane=f"%{index}", hive_id=base.hive_id + str(index)) for index in range(20)]
        self.controller.hive_agents = self.agents
        self.controller.agents = self.agents
        self.addCleanup(self.controller.shutdown)

    def test_visible_batch_limit_dormancy_and_retry_cadence(self):
        controller = self.controller
        controller.hive_visible = {agent.key for agent in self.agents[:18]}
        controller.cache[self.agents[0].key] = (99.5, "synthetic")
        controller.tick_hive(100, False)
        controller.hive_pool.submit.assert_not_called()
        controller.tick_hive(100, True)
        requested = controller.hive_pool.submit.call_args.args[1]
        self.assertEqual(len(requested), 16)
        self.assertEqual(requested, self.agents[1:17])
        controller.hive_capture.set_exception(HiveError("Hive command unavailable"))
        controller.tick_hive(100.1, False)
        self.assertTrue(all(agent.key in controller.hive_errors for agent in requested))
        controller.hive_visible = {requested[0].key}
        controller.tick_hive(100.5, True)
        self.assertEqual(controller.hive_pool.submit.call_count, 1)
        controller.tick_hive(101.1, True)
        self.assertEqual(controller.hive_pool.submit.call_count, 2)

    def test_stale_capture_requests_rediscovery_and_unknown_state(self):
        controller = self.controller
        agent = self.agents[0]
        controller.cache[agent.key] = (99, "old")
        controller.hive_states[agent.key] = (99, "idle", "heuristic")
        controller.hive_requested = [agent]
        controller.hive_capture = Future()
        controller.hive_capture.set_result({agent.key: (None, "stale", "unknown", "unavailable", "", 0)})
        controller.tick_hive(100, False)
        self.assertNotIn(agent.key, controller.cache)
        self.assertNotIn(agent.key, controller.hive_states)
        self.assertEqual(controller.hive_errors[agent.key], "stale")
        self.assertEqual(controller.next_hive_list, 0)
        controller.hive_pool.submit.assert_not_called()

    def test_old_generation_capture_does_not_repopulate_cache(self):
        controller = self.controller
        old = self.agents[0]
        new = replace(old, generation="2:2", hive_id=old.hive_id + "A")
        controller.hive_listing = Future()
        controller.hive_listing.set_result(([new], "Hive connected"))
        controller.hive_capture = Future()
        controller.hive_capture.set_result({old.key: ("old preview", "", "idle", "heuristic", "", 0)})
        controller.hive_requested = [old]
        controller.tick_hive(100, False)
        self.assertEqual(controller.hive_agents, [new])
        self.assertNotIn(old.key, controller.cache)
        self.assertNotIn(new.key, controller.cache)

    def test_success_keeps_hive_state_and_failed_inventory_is_explicit(self):
        controller = self.controller
        agent = self.agents[0]
        controller.hive_capture = Future()
        controller.hive_capture.set_result({agent.key: ("inconclusive", "", "waiting-permission", "shared", "", 0)})
        controller.hive_requested = [agent]
        controller.tick_hive(100, False)
        self.assertEqual(controller.hive_states[agent.key], (100, "waiting-permission", "shared"))
        controller.hive_listing = Future()
        controller.hive_listing.set_exception(HiveError("Hive operation timed out"))
        controller.tick_hive(101, False)
        self.assertEqual(controller.hive_health, "Hive operation timed out")
        self.assertFalse(controller.hive_agents)

    def test_inventory_shared_state_is_available_without_a_capture(self):
        controller = self.controller
        agent = replace(self.agents[0], state="thinking", provenance="shared")
        controller.hive_listing = Future()
        controller.hive_listing.set_result(([agent], "Hive connected"))
        controller.tick_hive(100, False)
        self.assertEqual(controller.hive_state(agent, 110), ("thinking", "Hive watcher"))
        self.assertEqual(controller.hive_state(agent, 116), ("unknown", "unavailable"))
        self.assertNotIn(agent.key, controller.cache)
        agent = replace(agent, state="unknown", provenance="unavailable", state_error="timeout")
        controller.hive_listing = Future()
        controller.hive_listing.set_result(([agent], "Hive connected"))
        controller.tick_hive(111, False)
        self.assertEqual(controller.hive_state(agent, 111), ("unknown", "unavailable"))
        self.assertEqual(controller.hive_state_errors[agent.key], "timeout")

    def test_delayed_capture_does_not_make_old_state_fresh(self):
        controller = self.controller
        agent = self.agents[0]
        controller.hive_requested = [agent]
        controller.hive_capture = Future()
        controller.hive_capture.set_result({agent.key: ("synthetic", "", "thinking", "shared", "", 14)})
        controller.tick_hive(100, False)
        self.assertEqual(controller.hive_state(agent, 100), ("thinking", "Hive watcher"))
        self.assertEqual(controller.hive_state(agent, 102), ("unknown", "unavailable"))


if __name__ == "__main__":
    unittest.main()
