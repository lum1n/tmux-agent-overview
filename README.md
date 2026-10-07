# tmux-agent-overview

A native tmux grid of live, read-only previews for **Copilot CLI, Claude Code,
Codex, Pi, OpenCode, and Cursor Agent**, across every session on the current
tmux server. Original agent panes stay where they are. Press Enter to work in
one, or zoom a preview without resizing its source.

When [Hive](https://github.com/lum1n/hive) is installed, the grid also includes
agents on its configured hosts and other tmux servers. Hive is entirely optional:
without it, the local overview works exactly as before.

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
Local inventory refreshes every two seconds and visible captures approximately every
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
When the watcher publishes subscription usage (`quota` events, Claude, Codex,
and Cursor), the grid's right status shows plan and usage per local agent kind,
e.g. `claude · Max 5x · 5h 33% · 7d 13%`, yellow from 80% and red when a
window is full. Local cards of an exhausted kind show `limit` with the time to
reset. The watcher reads the agents' own login files and contacts the vendors;
overview only displays its normalized percentages. Quota describes this
machine's accounts, so Hive cards never show it. Unknown watcher event types
are ignored rather than treated as a disconnect.
Unknown card states are displayed simply as `unknown`, without a provenance
suffix. Provenance is retained internally; known states still identify their
source.

### Optional Hive hosts

Overview automatically detects `hive` on the controller's PATH and checks its
version-1 agent API. No Hive daemon or remote Hive installation is required.
Configure hosts and SSH access in Hive normally; overview delegates discovery,
captures, authentication, and attachment to Hive rather than implementing SSH.
Install a Hive version supporting `hive agents capabilities --json`.

```tmux
# Default: automatically include Hive agents if the executable is available.
set -g @agent-overview-hive 'auto'

# Disable Hive discovery and its network requests.
set -g @agent-overview-hive 'off'

# Optional: use an explicit executable, including a locally built checkout.
set -g @agent-overview-hive-bin '/absolute/path/to/hive'
```

Use `on` instead of `auto` to report a missing executable as an error.
An installed but incompatible or failing Hive is reported in grid status;
failed hosts are not mistaken for healthy hosts with no agents. These options
are read when the controller starts. Close all overview views with `q`, then
reopen with `prefix-O` after changing them.

Hive cards include the host label, searchable with `/`. Current-server local
panes remain native cards and are not duplicated. Matching pane IDs on different
hosts or servers remain separate agents. Hive inventory refreshes approximately
every ten seconds while a grid is active. Only visible Hive agents are captured,
in batches of up to 16, approximately once per second. Updated Hive versions can
also provide existing remote watcher states in inventory and captures. Known
watcher states are labelled `Hive watcher`; off-screen state filters can use
fresh inventory states without capturing those panes. Without a matching
watcher, states fall back to Hive's text classification; inconclusive states
are shown simply as `unknown`. Optional watcher failures appear in status
without hiding working previews. Disable that bridge in Hive with
`agent_watcher = "off"` if desired; no remote watcher installation is required.
Two separate workers keep slow
hosts from occupying local discovery/capture workers. Captures and commands
have output limits, deadlines, and shutdown cancellation.

`Enter` opens a normal interactive attachment pane using Hive's exact agent
reference. Hive's detach shortcut (`Ctrl-space` by default) returns to the grid
without terminating the agent. `prefix-O` can return early; that attachment
stays dormant until the view is closed or another Hive attachment replaces it.
Closing/disconnecting removes owned attachment sessions. Preview zoom (`z`)
works for Hive cards; source zoom (`Z`) is unavailable through Hive's v1 API
and displays a message instead of changing a local pane.

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

Without Hive, discovery covers only the current tmux server. Agent
launching/termination, input broadcasting, auto-approval, and transcript storage
are not supported.

## Privacy and resource boundaries

Pane text and process arguments are sensitive: no telemetry, transcript exports,
on-disk preview cache, or content logging. There are no network requests without
Hive. When enabled, Hive contacts its configured hosts over SSH to retrieve
agent metadata and visible previews; use `@agent-overview-hive off` to keep
discovery strictly local. Hive retains responsibility for SSH configuration,
credentials, and host-key verification.
Standalone mode does not read agent session history files. Captures are kept
in memory and unsafe control sequences are stripped before rendering. Commands
use argument arrays; tmux format labels cannot execute source text. These
boundaries follow ASVS 1.2.5, 1.5.2, 2.2.1, 8.2.1, 13.2.6,
14.2.3-14.2.4, and 15.4.1-15.4.2.

Runtime sockets are under a private same-user directory in `$TMPDIR` (or the
platform temporary directory when unset). Existing foreign/unsafe resources
are rejected. Source snapshots are shared between views, at most 16 captures
are pending, and four workers handle subprocess work with deadlines. Unix
socket messages and captured content are bounded. Hive subprocesses have a
separate two-worker pool; each capture batch has at most 16 targets and each
preview at most 64 KiB. Remote states come from Hive's inventory/capture responses, never
the local window-level watcher.

Dependencies are tmux, Python's standard library, and the operating system's
`ps`. Install them from trusted OS/package-manager sources and apply security
updates promptly; address critical dependency vulnerabilities before release,
and review remaining updates at each release. Optional watcher compatibility
is tested against its versioned protocol rather than dynamically executing
downloaded harnesses. Hive is an optional, user-installed executable, checked
against its versioned API; overview never downloads or updates it.

## Development

```sh
python3 -m unittest discover -s tests/unit -v
python3 -m unittest discover -s tests/integration -v
python3 -m compileall -q src tests
bash -n tmux-agent-overview.tmux
```

Integration tests start unique disposable tmux servers with synthetic content,
not your live sessions. Do not use personal/customer output for test fixtures.
Hive integration tests compile a small synthetic executable with Go when it is
available; they never use your Hive configuration or contact real hosts.

Troubleshooting: missing Python or an occupied launch key produces a visible
error; ambiguous detection can use a pane override; watcher problems appear
in overview status. If Unix socket paths are too long, use a shorter private
`TMPDIR`. Run `python3 scripts/overview.py --help` for direct CLI usage.

## License

[MIT](LICENSE).
