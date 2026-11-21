"""Compatibility launch alias for the single PCB assistant MCP service.

Both historical launch commands expose only pcb_answer; diagnosis logic stays
in agent.workflow, without a second protocol implementation.
"""
from phase6_mcp import dispatch, list_tools, main, serve

__all__ = ["dispatch", "list_tools", "main", "serve"]

if __name__ == "__main__":
    raise SystemExit(main())
