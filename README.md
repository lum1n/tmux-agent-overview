# tmux-agent-overview

A native tmux grid of live, read-only previews for **Copilot CLI, Claude Code,
Codex, Pi, OpenCode, and Cursor Agent**, across every session on the current
tmux server. Original agent panes stay where they are. Press Enter to work in
one, or zoom a preview without resizing its source.

Python 3.10+ and tmux 3.2+ are required. No pip packages, compiled binaries,
browser, or separate watcher installation are needed. Native integration has
been exercised on tmux 3.6a and the minimum supported tmux 3.2 on macOS.
CI exercises both tmux versions on Linux and macOS.

## Installation

Add the plugin after TPM in your tmux configuration and install with `prefix-I`:

```tmux
set -g @plugin 'lum1n/tmux-agent-overview'
```

The repository's `.tmux` entrypoint must be executable in published Git metadata,
as required by TPM.

For a local checkout, or when deliberately using a non-executable entrypoint,
load it explicitly:

```tmux
run-shell 'bash /absolute/path/to/tmux-agent-overview/tmux-agent-overview.tmux'
```

This installs `prefix-O`. If that key is already bound, choose a free key before
loading the plugin:

```tmux
set -g @agent-overview-key 'G'
```

Reloading is idempotent. The plugin does not replace `prefix-s`, root bindings,
global statusline configuration, or other plugins' hooks. Grid shortcuts,
mouse behavior, and styling live in a private viewing session for each client.

## Controls

| Key | Action |
| --- | --- |
| `prefix-O` | Open or return to your grid |
| arrows / `h j k l` | Select a tile |
| `1`-`9`, `0` | Select tile 1-10 on this page |
| `Enter` | Focus the original agent pane |
| `z` | Zoom/unzoom the selected preview |
| `Z` | Focus and zoom the original agent |
| `[` / `]` | Previous/next page |
| `/` | Search agent kind, session, project, or state |
| `f` | Filter using a kind/state search term |
| `PageUp` / `PageDown`, wheel | Scroll a bounded preview history |
| `g` | Follow live output again |
| `?` | Show help |
| `Escape` | Unzoom, clear search, then close |
| `q` | Close and return to your saved origin |

Mouse click selects; it never forwards input into agents. A preview is clipped
terminal text, not a full interactive mirror. Images and arbitrary terminal
control sequences are not replayed. Direction keys select additional tiles
beyond the numeric shortcuts.

The grid adapts to available dimensions, with up to 12 tiles per page and
roughly 40x10 minimum tile size. Smaller terminals show one preview. Borders
include tile number, kind, textual state, provenance, and source identity.
Previews have a dark card surface and rounded, terminal-drawn borders, with
two columns of exterior spacing on each side and one row above and below.
Headings sit in the card border; content starts directly inside it. Native
tmux separators are hidden in the gutter, and the selected card has a bright
outline. Exterior spacing shrinks in small panes; very tiny panes omit the
border to keep content visible. Rounded corners use Unicode box-drawing
characters, not a graphical pixel radius.
Inventory refreshes every two seconds and visible captures approximately every
500 ms. A bounded rotating capture budget also classifies off-screen agents;
large inventories may temporarily show `unknown` until sampled.

## Detection and states

The plugin detects known commands and descendant interpreter wrappers, not old
agent text left in a shell. For wrappers it cannot identify, explicitly mark a
pane:

```sh
tmux set-option -p -t %12 @agent-overview-kind copilot
tmux set-option -p -t %13 @agent-overview-kind off
```

Kinds: `copilot`, `claude`, `codex`, `pi`, `opencode`, `cursor`. An explicit kind
override stays until removed; remove it when a marked pane no longer hosts an
agent. Generic `node`, `python`, or `agent` processes are not sufficient
evidence. A pane with multiple competing agent kinds requires an override.

Standalone states are conservative **heuristics** from recent visible output.
`unknown` is intentional when there is insufficient evidence. UI changes in
agent releases can require updating these rules; idle output is not evidence
that an agent process is still running.

### Alongside tmux-agent-state

Install [tmux-agent-state](https://github.com/lum1n/tmux-agent-state) normally.
When `@agent_watcher_socket` is available, overview subscribes to the existing
same-user, version-1 NDJSON stream. It does not start, stop, bind agents into,
or modify that daemon. Disconnects are visible and standalone previews continue.
Upstream agent-watcher now supports Copilot; shared Copilot states work when
your tmux-agent-state installation includes that updated watcher. Its upstream
pin now includes Copilot at `27538de1a608fd45f6f9046003c20822eedb35ab`.

Current watcher records are **window-level**, whereas tiles are **pane-level**.
Shared state is used only for a matching kind in a window with exactly one
detected agent, and only when that agent is the active pane. Multiple agents
in one window, an active shell instead of the agent, and unbound records use
local heuristics/unknown instead of attributing somebody else's state.

## Zoom, lifecycle, and limitations

Returning with the launch shortcut undoes source zoom introduced by the plugin
only if that pane is still active and no later native zoom/resize command
changed its ownership epoch. Preexisting zoom is preserved. Zoom and source
selection are shared tmux state, so other clients viewing the same source can
be affected. Switching sessions may also trigger tmux's normal automatic
window-sizing policy; overview never explicitly resizes source panes.

Each client gets its own view. The lazy controller pauses preview work when
the client works in an agent; closing the last view or disconnecting its owner
stops the controller and removes its socket. A small lock file may remain in
the private runtime directory to serialize future starts safely. Server exit
also ends the controller. After an unexpected controller failure, reopening
replaces only a session marked as owned by this plugin.

Multiple tmux servers, SSH aggregation, agent launching/termination, input
broadcasting, auto-approval, and transcript storage are not supported.

## Privacy and resource boundaries

Pane text and process arguments are sensitive: no telemetry, remote runtime
requests, transcript exports, on-disk preview cache, or content logging.
Standalone mode does not read agent session history files. Captures are kept
in memory and unsafe control sequences are stripped before rendering. Commands
use argument arrays; tmux format labels cannot execute source text. These
boundaries follow ASVS 1.2.5, 2.2.1, 8.2.1, and 14.2.3-14.2.4.

Runtime sockets are under a private same-user directory in `$TMPDIR` (or the
platform temporary directory when unset). Existing foreign/unsafe resources
are rejected. Source snapshots are shared between views, at most 16 captures
are pending, and four workers handle subprocess work with deadlines. Unix
socket messages and captured content are bounded.

Dependencies are tmux, Python's standard library, and the operating system's
`ps`. Install them from trusted OS/package-manager sources and apply security
updates promptly; address critical dependency vulnerabilities before release,
and review remaining updates at each release. Optional watcher compatibility
is tested against its versioned protocol rather than dynamically executing
downloaded harnesses.

## Development

```sh
python3 -m unittest discover -s tests/unit -v
python3 -m unittest discover -s tests/integration -v
python3 -m compileall -q src tests
bash -n tmux-agent-overview.tmux
```

Integration tests start unique disposable tmux servers with synthetic content,
not your live sessions. Do not use personal/customer output for test fixtures.

Troubleshooting: missing Python or an occupied launch key produces a visible
error; ambiguous detection can use a pane override; watcher problems appear
in overview status. If Unix socket paths are too long, use a shorter private
`TMPDIR`. Run `python3 scripts/overview.py --help` for direct CLI usage.

## License

[MIT](LICENSE).
