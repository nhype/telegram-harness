#!/usr/bin/env python3
"""A tiny OpenAI-compatible endpoint that answers every chat completion with "[SILENT]".

Lets the integration test run a real controller turn without an LLM account.
usage: python3 tests/fake_llm.py PORT
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "[SILENT]"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _json(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # /v1/models
        self._json({"object": "list", "data": [{"id": "fake", "object": "model", "owned_by": "test"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not request.get("stream"):
            self._json({"id": "fake-1", "object": "chat.completion", "created": 0, "model": "fake",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": ANSWER}}],
                        "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in (({"role": "assistant", "content": ANSWER}, None), ({}, "stop")):
            chunk = {"id": "fake-1", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if finish:
                chunk["usage"] = usage
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
