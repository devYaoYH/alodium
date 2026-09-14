"""Offline OpenAI-compatible model server for jail-image tests.

    python3 mock-openai.py [TOOL_NAME TOOL_ARGS_JSON]

Listens on 127.0.0.1:8765. With a tool given, the first chat request that
carries tools gets back one call to it; once a tool result is in the
conversation (or with no tool given) the reply is plain "done". Streaming and
non-streaming both work. Lets scripts/test-jail-image.sh drive forge through a
real tool call with --network none — no provider, no key, no spend.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TOOL = sys.argv[1] if len(sys.argv) > 1 else ""
ARGS = sys.argv[2] if len(sys.argv) > 2 else "{}"
MODEL = "deepseek-flash"


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
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        answered = any(m.get("role") == "tool" for m in req.get("messages", []))
        if TOOL and req.get("tools") and not answered:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": "call_1", "type": "function", "function": {"name": TOOL, "arguments": ARGS}}]}
            finish = "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": "done"}, "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        base = {"id": "mock", "created": 0, "model": MODEL}

        if not req.get("stream"):
            self._json({**base, "object": "chat.completion", "usage": usage,
                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for choice, extra in (({"index": 0, "delta": msg, "finish_reason": None}, {}),
                              ({"index": 0, "delta": {}, "finish_reason": finish}, {"usage": usage})):
            chunk = {**base, "object": "chat.completion.chunk", "choices": [choice], **extra}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
