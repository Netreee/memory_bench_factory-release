#!/usr/bin/env python3
"""stdio MCP：只暴露 grep_memory + read_file，与 Agents SDK 同一套 MemoryStore。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_ROOT))

from memory_store import MemoryStore  # noqa: E402


TOOLS = [
    {
        "name": "grep_memory",
        "description": (
            "在全部记忆文档中检索关键词，返回带周期和相对路径的片段。"
            "可搜人名、实体、字段。先用这个定位，再 read_file。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "实体名、字段名或关键词"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "按相对路径读取一篇记忆文档，例如 sessions/s03_2025-01-20/12_公告_xxx.md。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的路径"}
            },
            "required": ["path"],
        },
    },
]


def _read_message() -> dict | None:
    header = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, val = line.decode("utf-8", errors="replace").partition(":")
        header[key.strip().lower()] = val.strip()
    n = int(header.get("content-length") or 0)
    if n <= 0:
        return None
    raw = sys.stdin.buffer.read(n)
    return json.loads(raw.decode("utf-8"))


def _write_message(msg: dict) -> None:
    blob = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(blob)}\r\n\r\n".encode("ascii") + blob)
    sys.stdout.buffer.flush()


def _result(req_id, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}]},
    }


def main() -> int:
    workspace = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    store = MemoryStore.from_workspace(workspace)
    while True:
        msg = _read_message()
        if msg is None:
            return 0
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "memory", "version": "1"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _write_message({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            name = (msg.get("params") or {}).get("name")
            if name == "grep_memory":
                text = store.grep_text(str(args.get("query") or ""))
            elif name == "read_file":
                text = store.read_file_text(str(args.get("path") or ""))
            else:
                text = f"(未知工具: {name})"
            _write_message(_result(req_id, text))
        elif method == "ping":
            _write_message({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif req_id is not None:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
