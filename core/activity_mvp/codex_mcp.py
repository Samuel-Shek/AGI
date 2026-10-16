"""Local STDIO MCP adapter. No listener, background polling, or raw data tools."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys

from .codex_bridge import CodexConsumer, RULES, record_provided, _ref
from .context_console import ContractError
from .storage import connect


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string", "maxLength": 256}
TOOLS = [
    {"name": "pam_status", "description": "Read availability of owner-approved PAM summaries; never starts recording.", "inputSchema": _schema({})},
    {"name": "pam_projects", "description": "List only projects with explicitly shared, currently valid summaries.", "inputSchema": _schema({})},
    {"name": "pam_search_context", "description": "When the user asks to resume a task or locate history, read only previously owner-confirmed and explicitly shared summaries. " + RULES,
     "inputSchema": _schema({"project_key": STRING, "query": STRING, "start_at": STRING, "end_at": STRING,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["project_key"])},
    {"name": "pam_acknowledge_use", "description": "Only after actually using and citing a returned reference, acknowledge that exact package. This is consumer self-report, not owner helpfulness or a memory write.",
     "inputSchema": _schema({"ticket": STRING, "cited_references": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 50}}, ["ticket", "cited_references"])},
]
for tool in TOOLS:
    tool["annotations"] = {"readOnlyHint": tool["name"] != "pam_acknowledge_use", "destructiveHint": False, "openWorldHint": False}


class Adapter:
    def __init__(self, database):
        self.database = Path(database).resolve()
        self.consumer = CodexConsumer()

    def call(self, name, arguments):
        schema = next((t["inputSchema"] for t in TOOLS if t["name"] == name), None)
        if schema is None or not isinstance(arguments, dict) or set(arguments) - set(schema["properties"]) or not set(schema["required"]) <= set(arguments):
            raise ValueError("unsupported_tool_or_arguments")
        for key, value in arguments.items():
            definition = schema["properties"][key]
            kind = definition["type"]
            if kind == "string" and (not isinstance(value, str) or len(value) > 256):
                raise ValueError("invalid_argument")
            if kind == "integer" and (type(value) is not int or not 1 <= value <= 10):
                raise ValueError("invalid_argument")
            if kind == "array" and (not isinstance(value, list) or not 1 <= len(value) <= 50
                                    or any(not isinstance(x, str) or len(x) > 256 for x in value)):
                raise ValueError("invalid_argument")
        meta = self.database.stat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise ValueError("private_database_unavailable")
        conn = connect(self.database, busy_timeout_seconds=0.25)
        try:
            if name == "pam_status":
                return self.consumer.status(conn)
            if name == "pam_projects":
                return self.consumer.projects(conn)
            if name == "pam_search_context":
                return self.consumer.search(conn, **arguments)
            return self.consumer.acknowledge(conn, **arguments)
        finally:
            conn.close()

    def response_sent(self, value):
        keys = []
        for result in value.get("results", []):
            ticket = self.consumer.tickets.get(result.get("ticket"))
            if ticket:
                keys.append(_ref("shared", ticket[1]["delivery_ref"]))
        if keys:
            conn = connect(self.database, busy_timeout_seconds=0.05)
            try:
                record_provided(conn, keys)
            finally:
                conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    adapter = Adapter(args.db)

    def timed_out(signum, frame):
        raise TimeoutError("request_deadline")

    signal.signal(signal.SIGALRM, timed_out)
    while True:
        line = sys.stdin.buffer.readline(65537)
        if not line:
            return
        if len(line) > 65536:
            return
        request = {}
        provided_value = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("invalid_request")
            if "id" not in request:
                continue
            method = request.get("method")
            signal.setitimer(signal.ITIMER_REAL, 4.0)
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "pam-approved-context", "version": "0.5.1"}, "instructions": RULES}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params", {})
                try:
                    value = adapter.call(params.get("name"), params.get("arguments", {}))
                    if params.get("name") == "pam_search_context":
                        provided_value = value
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
                except (ValueError, ContractError, sqlite3.Error, OSError, TimeoutError, TypeError):
                    # Never serialize exception text: file paths, SQL fragments,
                    # and caller-provided input must stay out of tool results.
                    result = {"content": [{"type": "text", "text": json.dumps({"status": "unavailable", "reason_code": "invalid_expired_revoked_or_busy", "raw_access": False})}], "isError": True}
            else:
                raise ValueError("method_not_found")
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid or unavailable request"}}
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if provided_value is not None:
            try:
                adapter.response_sent(provided_value)
            except (sqlite3.Error, OSError, ValueError):
                pass  # A failed metric write never invents a successful delivery.


if __name__ == "__main__":
    main()
