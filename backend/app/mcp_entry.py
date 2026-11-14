"""MCP CLI discovery entry point. Imported by `mcp dev app/mcp_entry.py`."""
import os

from app.mcp_server import create_server
from app.service import Gateway

mcp = create_server(Gateway(), os.environ.get("MCP_AGENT_ID", "mcp-agent"))
