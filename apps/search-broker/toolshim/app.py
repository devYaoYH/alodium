#!/usr/bin/env python3
"""search-broker-toolshim: chat-facing search tool for Open WebUI.

Open WebUI registers this as an OpenAPI tool server (wired declaratively by
scripts/chat-tools-setup.sh from manifest/search-broker.toml [expose.chat]);
models then call the search endpoint mid-conversation.

Upstream: the search-broker on the search-chat network, authenticated with
CHAT_SEARCH_TOKEN (separate from the agent-dev credential). stdlib only — no dependencies to mirror.
"""
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEARCH_BROKER_URL = os.environ.get("SEARCH_BROKER_URL", "http://search-broker:8080")
SEARCH_TOKEN = os.environ.get("CHAT_SEARCH_TOKEN", "")
PORT = int(os.environ.get("PORT", "8101"))

OPENAPI = {
    "openapi": "3.1.0",
    "info": {
        "title": "search-broker-tools",
        "version": "1.0.0",
        "description": "Web search via Exa, audited by the node's search-broker.",
    },
    "paths": {
        "/v1/search": {
            "post": {
                "operationId": "web_search",
                "summary": "Search the web via Exa",
                "description": "Performs a web search using the Exa API. "
                "Returns a list of results with titles, URLs, and snippets.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["query"],
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "The search query",
                                    },
                                    "num_results": {
                                        "type": "integer",
                                        "description": "Number of results (1-25)",
                                        "default": 10,
                                        "maximum": 25,
                                    },
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Search results",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object"},
                            }
                        },
                    }
                },
            }
        },
    },
}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/openapi.json":
            self.send_json(OPENAPI)
        elif self.path == "/healthz":
            self.send_json({"status": "ok", "upstream": bool(SEARCH_TOKEN)})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path == "/v1/search":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            try:
                params = json.loads(body)
            except json.JSONDecodeError as e:
                self.send_json({"error": f"invalid JSON: {e}"}, 400)
                return

            query = (params.get("query") or "").strip()
            if not query:
                self.send_json({"error": "query is required"}, 400)
                return

            num_results = min(int(params.get("num_results", 10) or 10), 25)

            try:
                payload = json.dumps({
                    "query": query,
                    "num_results": num_results,
                }).encode()
                req = urllib.request.Request(
                    f"{SEARCH_BROKER_URL}/v1/search",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {SEARCH_TOKEN}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.load(resp)
                self.send_json(data)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                self.send_json(
                    {"error": f"search-broker error {e.code}: {detail}"}, e.code
                )
            except urllib.error.URLError as e:
                self.send_json(
                    {"error": f"search-broker unreachable: {e.reason}"}, 502
                )
            except Exception as e:
                self.send_json({"error": f"search failed: {e}"}, 502)
        else:
            self.send_json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(
        f"search-broker-toolshim on :{PORT} -> {SEARCH_BROKER_URL} "
        f"(token {'set' if SEARCH_TOKEN else 'MISSING'})",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
