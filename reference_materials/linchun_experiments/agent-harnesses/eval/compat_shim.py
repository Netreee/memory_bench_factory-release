"""本地转发：给 Claude Code 对接 luna。

Claude 打 /v1/messages，网关再转成带 reasoning_effort 的 chat/completions，luna 会 400。
这里改成本地译成 /v1/chat/completions，并强制 reasoning_effort=none。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


_server: ThreadingHTTPServer | None = None
_lock = threading.Lock()


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") in {"text", "input_text"} and b.get("text"):
            parts.append(str(b["text"]))
        elif b.get("type") == "tool_result":
            parts.append(str(b.get("content") or ""))
    return "\n".join(parts)


def anthropic_to_chat(data: dict) -> dict:
    msgs: list[dict] = []
    system = data.get("system")
    if isinstance(system, str) and system.strip():
        msgs.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = _block_text(system)
        if text:
            msgs.append({"role": "system", "content": text})
    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        texts, tool_calls, tool_results = [], [], []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype in {"text", "input_text"}:
                texts.append(str(b.get("text") or ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": b.get("id") or "call_0",
                    "type": "function",
                    "function": {
                        "name": b.get("name") or "",
                        "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id") or "",
                    "content": _block_text(b.get("content")) if not isinstance(b.get("content"), str) else b.get("content") or "",
                })
        if role == "assistant" and tool_calls:
            msgs.append({
                "role": "assistant",
                "content": "\n".join(texts) or None,
                "tool_calls": tool_calls,
            })
        elif tool_results:
            msgs.extend(tool_results)
        else:
            msgs.append({"role": role, "content": "\n".join(texts)})
    tools = []
    for t in data.get("tools") or []:
        if not isinstance(t, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    out = {
        "model": data.get("model"),
        "messages": msgs,
        "reasoning_effort": "none",
    }
    if tools:
        out["tools"] = tools
        out["tool_choice"] = "auto"
    max_tokens = data.get("max_tokens")
    if max_tokens:
        out["max_completion_tokens"] = max_tokens
    return out


def chat_to_anthropic(data: dict) -> dict:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    text = msg.get("content")
    if isinstance(text, str) and text.strip():
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = {"_raw": raw}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or "call_0",
            "name": fn.get("name") or "",
            "input": args,
        })
    if not content:
        content = [{"type": "text", "text": ""}]
    finish = choice.get("finish_reason") or "stop"
    stop = "tool_use" if finish == "tool_calls" else "end_turn"
    usage = data.get("usage") or {}
    return {
        "id": data.get("id") or "msg_shim",
        "type": "message",
        "role": "assistant",
        "model": data.get("model"),
        "content": content,
        "stop_reason": stop,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


def _rewrite_chat_body(raw: bytes) -> bytes:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    if not isinstance(data, dict):
        return raw
    if data.get("tools"):
        data["reasoning_effort"] = "none"
    data.pop("temperature", None)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _host(upstream_base: str) -> str:
    host = upstream_base.rstrip("/")
    if host.endswith("/v1"):
        host = host[:-3]
    return host


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: ARG002
        return

    def _write_anthropic_sse(self, anth: dict) -> None:
        chunks: list[str] = []

        def ev(name: str, data: dict) -> None:
            chunks.append(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

        ev("message_start", {
            "type": "message_start",
            "message": {
                "id": anth.get("id"),
                "type": "message",
                "role": "assistant",
                "model": anth.get("model"),
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": (anth.get("usage") or {}).get("input_tokens") or 0, "output_tokens": 0},
            },
        })
        for i, block in enumerate(anth.get("content") or []):
            ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {**block, "text": ""} if block.get("type") == "text" else block})
            if block.get("type") == "text":
                ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": block.get("text") or ""},
                })
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": anth.get("stop_reason") or "end_turn"},
            "usage": {"output_tokens": (anth.get("usage") or {}).get("output_tokens") or 0},
        })
        ev("message_stop", {"type": "message_stop"})
        payload = "".join(chunks).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write(self, code: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _post_json(self, url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
        hdrs = dict(headers)
        hdrs["Content-Type"] = "application/json"
        hdrs["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _forward(self) -> None:
        host = _host(getattr(self.server, "upstream_base", ""))
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in {"host", "content-length"}
        }
        key = headers.get("x-api-key") or headers.get("X-Api-Key")
        if key and "Authorization" not in headers and "authorization" not in {k.lower() for k in headers}:
            headers["Authorization"] = f"Bearer {key}"

        if self.command == "POST" and path.endswith("/messages") and body:
            try:
                src = json.loads(body.decode("utf-8"))
                want_stream = bool(src.get("stream"))
                chat = anthropic_to_chat(src)
                raw = json.dumps(chat, ensure_ascii=False).encode("utf-8")
            except Exception as e:
                self._write(400, json.dumps({"error": {"message": f"shim_convert:{e}"}}).encode())
                return
            code, payload = self._post_json(host + "/v1/chat/completions", raw, headers)
            if code != 200:
                self._write(code, payload)
                return
            try:
                anth = chat_to_anthropic(json.loads(payload))
            except Exception as e:
                self._write(502, json.dumps({"error": {"message": f"shim_resp:{e}"}}).encode())
                return
            if want_stream:
                self._write_anthropic_sse(anth)
            else:
                self._write(200, json.dumps(anth, ensure_ascii=False).encode())
            return

        if body and path.endswith("/chat/completions"):
            body = _rewrite_chat_body(body)
        url = host + self.path
        if body is not None:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in {"transfer-encoding", "connection"}:
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            self._write(e.code, e.read(), e.headers.get("Content-Type", "application/json"))
        except Exception as e:
            self._write(502, json.dumps({"error": {"message": f"shim:{type(e).__name__}"}}).encode())

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()


def ensure_shim(upstream_base: str) -> str:
    """启动转发，返回 OpenAI 风格 base（含 /v1）。Anthropic 根是去掉 /v1。"""
    global _server
    with _lock:
        if _server is not None:
            port = _server.server_address[1]
            return f"http://127.0.0.1:{port}/v1"
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.upstream_base = upstream_base.rstrip("/")
        thread = threading.Thread(target=httpd.serve_forever, name="openai-compat-shim", daemon=True)
        thread.start()
        _server = httpd
        port = httpd.server_address[1]
        return f"http://127.0.0.1:{port}/v1"
