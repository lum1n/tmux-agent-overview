# Changelog

## 0.2.2

- Loading the plugin from another checkout replaces that checkout's launch
  binding instead of refusing it; switching from a local copy to TPM's no
  longer leaves the old code bound to `prefix-O`.
- Install failures (such as a launch key owned by something else) are shown
  in tmux; TPM discards plugin output, so they were previously silent.

## 0.2.1

- Fit the status bar to the window: key hints shorten, then health/page text,
  then meters compact (plan and later windows dropped) and finally collapse to
  `+N`. Before, tmux cut quota meters from the start when the left side was long.

## 0.2.0

- Quota for Hive hosts: when Hive reports a server watcher's `quota`, the
  status line shows a meter per host and kind, and that host's cards show
  `limit` when exhausted. Requires a Hive build with per-server quota; remote
  watcher states also need it after agent-watcher started replaying quota.

## 0.1.0

- Native tmux grid of live, read-only previews for Copilot CLI, Claude Code,
  Codex, Pi, OpenCode, and Cursor Agent across every session on the server.
- Focus, preview zoom, source zoom, paging, search/filter, and bounded
  scrollback; original panes are never resized or moved.
- Conservative heuristic states, or shared states from
  [tmux-agent-state](https://github.com/lum1n/tmux-agent-state)'s watcher socket.
- Optional [Hive](https://github.com/lum1n/hive) integration: agents on other
  hosts and tmux servers, with remote watcher states and interactive attach.
- Subscription quota from the watcher (tmux-agent-state 0.2.0+): plan and
  usage per agent kind in the status line, and `limit` on cards of an
  exhausted kind. Unknown watcher event types no longer drop the connection.
