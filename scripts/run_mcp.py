#!/usr/bin/env python3
"""Stdio entry point for the BetterPlace Co-Pilot MCP server.

Claude Desktop spawns this script per its `claude_desktop_config.json`.
The MCP server then communicates over stdin/stdout with Claude Desktop.

Run locally for a quick smoke test:
    python scripts/run_mcp.py
(then send Ctrl-D to exit; Claude Desktop sends real JSON-RPC messages over stdin)
"""
from __future__ import annotations
import sys
from pathlib import Path

# Make `from src ...` imports work when launched directly by Claude Desktop
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
