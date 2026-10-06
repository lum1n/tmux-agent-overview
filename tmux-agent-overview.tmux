#!/usr/bin/env bash
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    tmux display-message 'agent-overview: Python 3.10+ is required'
    exit 1
fi
python3 "$ROOT/scripts/overview.py" install
