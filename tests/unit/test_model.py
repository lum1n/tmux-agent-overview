import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from tmux_agent_overview.discovery import detect, descendant_kind
from tmux_agent_overview.model import Agent, KINDS, capacity, card_content_size, card_frame, card_margin, classify, clean, clip, label, native_layout
from tmux_agent_overview.watcher import Watcher


class ModelTests(unittest.TestCase):
    def test_six_kinds(self):
        for command, kind in zip(("copilot", "claude", "codex", "pi", "opencode", "cursor-agent"), KINDS):
            self.assertEqual(detect(command), kind)
            self.assertEqual(detect("/usr/local/bin/" + command), kind)

    def test_wrappers(self):
        self.assertEqual(detect("node /x/@github/copilot/index.js"), "copilot")
        self.assertEqual(detect("node /x/pi-coding-agent/index.js"), "pi")
        for command in ("node", "python", "agent", "echo copilot", "not-claude"):
            self.assertIsNone(detect(command))

    def test_descendants(self):
        self.assertEqual(descendant_kind(1, {1: [2], 2: [3]}, {1: "bash", 2: "claude", 3: "git"}), "claude")
        self.assertIsNone(descendant_kind(1, {1: [2, 3]}, {2: "claude", 3: "codex"}))

    def test_mainthread_requires_supported_module(self):
        for args, kind in (("node /x/@github/copilot/index.js", "copilot"), ("node /x/application.js", None)):
            with mock.patch("tmux_agent_overview.discovery.subprocess.run",
                            return_value=mock.Mock(returncode=0, stdout=args)):
                self.assertEqual(descendant_kind(1, {1: [2], 2: [3]},
                                                 {1: "bash", 2: "bwrap", 3: "MainThread"}), kind)

    def test_control_sequences(self):
        text = "\x1b]52;c;clipboard\x07hello\x1b[31m!\x1b[0m\x00"
        self.assertEqual(clean(text), "hello!")
        self.assertEqual(label("#{run-shell:evil}"), "_{run-shell:evil}")

    def test_cell_clipping(self):
        self.assertEqual(clip("中文ab", 5), "中文a")
        self.assertEqual(clip("e\u0301x", 1), "e\u0301")

    def test_card_margin_and_content_size(self):
        self.assertEqual(card_margin(40, 10), (2, 1))
        self.assertEqual(card_content_size(40, 10), (34, 6))
        self.assertEqual(card_margin(4, 3), (0, 0))
        self.assertEqual(card_content_size(4, 3), (2, 1))
        self.assertEqual(card_margin(1, 1), (0, 0))
        self.assertEqual(card_content_size(1, 1), (1, 1))

    def test_card_frame_gutters_and_clipping(self):
        frame = card_frame(["abcdefghij", "second", "third"], 12, 6, heading="agent")
        self.assertIn("\x1b[38;5;252;48;5;235m", frame)
        self.assertIn("\x1b[38;5;252;48;5;234m\x1b[H\x1b[2J", frame)
        self.assertIn("╭──────╮", frame)
        self.assertIn("╰──────╯", frame)
        self.assertIn("\x1b[3;4Habcdef", frame)
        self.assertIn("\x1b[4;4Hsecond", frame)
        self.assertNotIn("third", frame)
        self.assertNotIn("ghij", frame)
        self.assertIn("\x1b[38;5;238;48;5;234m", frame)
        self.assertIn("\x1b[38;5;81;48;5;234m",
                      card_frame([], 12, 6, selected=True))

    def test_card_frame_tiny_and_unicode(self):
        self.assertIn("\x1b[1;1Hx", card_frame(["xyz"], 1, 1))
        self.assertIn("\x1b[3;4H中文a", card_frame(["中文ab"], 11, 5))
        self.assertNotIn("\x1b]52", card_frame(["\x1b]52;c;clipboard\x07safe"], 40, 10))
        self.assertNotIn("\x1b]52", card_frame([], 40, 10, heading="\x1b]52;c;clipboard\x07safe"))

    def test_card_geometry_stays_inside_small_panes(self):
        for width in range(1, 13):
            for height in range(1, 9):
                with self.subTest(width=width, height=height):
                    content_width, content_height = card_content_size(width, height)
                    self.assertGreaterEqual(content_width, 1)
                    self.assertGreaterEqual(content_height, 1)
                    frame = card_frame(["x" * 20] * 10, width, height, heading="long heading")
                    for y, x in re.findall(r"\x1b\[(\d+);(\d+)H", frame):
                        self.assertTrue(1 <= int(y) <= height)
                        self.assertTrue(1 <= int(x) <= width)

    def test_states(self):
        for kind in KINDS:
            self.assertEqual(classify(kind, "Do you want to allow this action?"), "waiting-permission")
            self.assertEqual(classify(kind, "Error: request failed"), "errored")
            self.assertEqual(classify(kind, "Working... esc to interrupt"), "thinking")
            self.assertEqual(classify(kind, "running tool"), "running-tool")
            self.assertEqual(classify(kind, "inconclusive output"), "unknown")
        self.assertEqual(classify("claude", "old permission required\ncompleted\n>\n"), "idle")

    def test_capacity(self):
        self.assertEqual(capacity(80, 24), 2)
        self.assertEqual(capacity(160, 48), 12)
        self.assertEqual(capacity(10, 4), 1)
        for count in (0, 1, 6, 25, 100):
            self.assertGreaterEqual(max(1, (count + capacity(80, 24) - 1) // capacity(80, 24)), 1)

    def test_layout(self):
        self.assertRegex(native_layout(["%1", "%2"], 80, 24), r"^[0-9a-f]{4},80x24,0,0\[")

    def test_watcher_schema_and_mapping(self):
        watcher = Watcher()
        watcher.event({"v": 1, "type": "state", "session": "dev", "window": 0, "kind": "claude", "state": "thinking"})
        agent = Agent("%1", "@1", "$1", "dev", "0", "claude", 1, True, "/project", "claude")
        self.assertEqual(watcher.state(agent, [agent]), "thinking")
        self.assertIsNone(watcher.state(agent, [agent, agent]))
        agent.active = False
        self.assertIsNone(watcher.state(agent, [agent]))
        for event in ({}, {"v": 2, "type": "hello"}, {"v": 1, "type": "snapshot", "agents": "bad"}):
            with self.assertRaises(ValueError):
                watcher.event(event)
        watcher.close()
        self.assertFalse(watcher.records)

    def test_shared_copilot_states(self):
        watcher = Watcher()
        agent = Agent("%2", "@2", "$2", "copilot-dev", "1", "copilot", 2, True, "/project", "copilot")
        for state in ("idle", "thinking", "running-tool", "waiting-permission", "errored"):
            watcher.event({"v": 1, "type": "snapshot", "agents": [
                {"session": "copilot-dev", "window": 1, "kind": "copilot", "state": state}]})
            self.assertEqual(watcher.state(agent, [agent]), state)
        watcher.close()


if __name__ == "__main__":
    unittest.main()
