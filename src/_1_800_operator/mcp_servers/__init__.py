"""Bundled MCP servers shipped with operator.

These run as separate stdio subprocesses spawned by the LLM (Claude Code,
or operator's own mcp_client), exposing operator-internal data — the live
meeting record, etc. — as tools the model can call on demand.
"""
