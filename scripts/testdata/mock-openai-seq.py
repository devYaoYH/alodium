"""Offline OpenAI-compatible model server that walks forge through a scripted
sequence of tool calls — one per model turn — then answers "done".

    python3 mock-openai-seq.py PLAN_JSON [REQUEST_LOG]

PLAN_JSON is a JSON list of {"name": <tool>, "args": {...}} (inline JSON or a
file path). Listens on 127.0.0.1:8765, like mock-openai.py.

Every chat request is appended to REQUEST_LOG (default /tmp/mock_requests.jsonl)
as one row in the shape scripts/trace-render.py reads from LiteLLM's spend logs
(start_us, end_us, has_tools, tool_calls, ...), so a run against this mock can
be rendered offline with `trace-render.py --requests-json`. Lets
scripts/test-jail-image.sh check measured tool timing with --network none —
no provider, no key, no spend.
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

PLAN_ARG = sys.argv[1] if len(sys.argv) > 1 else "[]"
PLAN = json.load(open(PLAN_ARG)) if os.path.isfile(PLAN_ARG) else json.loads(PLAN_ARG)
LOG = sys.argv[2] if len(sys.argv) > 2 else "/tmp/mock_requests.jsonl"
MODEL = "deepseek-flash"


def now_us():
    return time.time_ns() // 1000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /v1/models
        self._json({"object": "list", "data": [{"id": MODEL, "object": "model", "created": 0, "owned_by": "mock"}]})

    def do_POST(self):  # /v1/chat/completions
        start = now_us()
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        step = sum(1 for m in req.get("messages", []) if m.get("role") == "tool")
        calls = []
        if req.get("tools") and step < len(PLAN):
            call = PLAN[step]
            calls = [{"name": call["name"], "args": json.dumps(call.get("args", {}))}]
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": f"call_{step}", "type": "function",
                 "function": {"name": calls[0]["name"], "arguments": calls[0]["args"]}}]}
            finish = "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": "done"}, "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        base = {"id": "mock", "created": 0, "model": MODEL}

        if not req.get("stream"):
            self._json({**base, "object": "chat.completion", "usage": usage,
                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}]})
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for choice, extra in (({"index": 0, "delta": msg, "finish_reason": None}, {}),
                                  ({"index": 0, "delta": {}, "finish_reason": finish}, {"usage": usage})):
                chunk = {**base, "object": "chat.completion.chunk", "choices": [choice], **extra}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        end = now_us()
        with open(LOG, "a") as f:
            f.write(json.dumps({
                "request_id": f"mock-{start}", "start_us": start, "first_token_us": end, "end_us": end,
                "model": MODEL, "prompt_tokens": 10, "completion_tokens": 2, "spend": 0.0,
                "status": "success", "has_tools": bool(req.get("tools")), "tool_calls": calls or None,
            }) + "\n")


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


Server(("127.0.0.1", 8765), Handler).serve_forever()
