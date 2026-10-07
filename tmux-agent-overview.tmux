#!/usr/bin/env bash
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    tmux display-message 'agent-overview: Python 3.10+ is required'
    exit 1
fi
# TPM discards plugin output, so report install failures in tmux itself.
if ! error="$(python3 "$ROOT/scripts/overview.py" install 2>&1)"; then
    tmux display-message "${error:-agent-overview: install failed}"
    exit 1
fi
