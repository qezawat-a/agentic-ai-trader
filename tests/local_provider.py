"""A minimal OpenAI-compatible server used to prove the provider path.

This is a REAL HTTP server: the agent talks to it over a real socket with
real JSON, the same way it talks to OpenRouter/OpenAI. It lets us verify
AutoSetModelByKey (list -> probe -> pick -> pin) end to end without a
paid provider key.

The models it advertises are deliberately MIXED:
  * good-model      - works, answers properly
  * empty-model     - returns HTTP 500 with an empty-content message
  * slow-model      - answers, but slowly (probe timeout territory)
  * broken-model    - always HTTP 401
  * trash-model     - HTTP 500 on every call
The selector must skip everything that does not work and pin good-model.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [
    {"id": "trash-model", "object": "model", "owned_by": "local"},
    {"id": "broken-model", "object": "model", "owned_by": "local"},
    {"id": "empty-model", "object": "model", "owned_by": "local"},
    {"id": "good-model", "object": "model", "owned_by": "local",
     "pricing": {"prompt": "0.0", "completion": "0.0"}},
    {"id": "slow-model", "object": "model", "owned_by": "local"},
]

CALLS: list[str] = []
_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the access log
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": MODELS})
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        model = payload.get("model", "")
        with _LOCK:
            CALLS.append(model)

        if model == "broken-model":
            self._json(401, {"error": {"message": "invalid api key"}})
            return
        if model == "trash-model":
            self._json(500, {"error": {"message": "internal server error"}})
            return
        if model == "empty-model":
            self._json(500, {"error": {"message": "empty response content"}})
            return
        if model == "slow-model":
            time.sleep(30)
            self._json(200, {"choices": [{"message": {"content": "late"}}]})
            return
        # good-model
        self._json(200, {
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant",
                            "content": "J+Rock here. The provider path works."},
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 9,
                      "total_tokens": 21},
        })


def serve() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address[0], srv.server_address[1]
    return srv, f"http://{host}:{port}/v1"
