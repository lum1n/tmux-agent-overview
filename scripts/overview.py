#!/usr/bin/env python3
import pathlib
import sys

if sys.version_info < (3, 10):
    sys.exit("agent-overview: Python 3.10+ is required")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from tmux_agent_overview.cli import main

main()
