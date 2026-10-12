#!/usr/bin/env python3
"""
A tiny OpenAI-compatible mock server, for testing heatmap_report.py without a
real model. Standard library only.

It implements the four endpoints the app touches, records every request it
receives, and answers with a canned heat-map reading so the whole pipeline can
be exercised end to end.

    python mock_openai_server.py --port 8099 --vision no     # simulate a text-only server
    python mock_openai_server.py --port 8099 --vision yes    # simulate a vision server

Then point the app at it:
    python heatmap_report.py --base http://127.0.0.1:8099/v1 --image shot.png

Every request body is appended to requests.jsonl next to this file, which is how
you inspect the exact payload shape the app produces.
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "requests.jsonl"

CANNED = """STRONGEST SECTORS: Technology and Communication Services.
WEAKEST SECTORS: Energy and Utilities.
NOTABLE TICKERS: unreadable at this resolution.
CONFIDENCE: low - this is a mock server, it cannot actually see your image.
UNREADABLE: every tile; no image was analysed.
"""

VISION = True
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {"status": "ok", "service": "mock", "images": VISION})
        elif path in ("/v1/models", "/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "mock-vision-model",
                            "object": "model",
                            "architecture": {
                                "input_modalities": ["text", "image"]
                                if VISION
                                else ["text"],
                                "output_modalities": ["text"],
                            },
                        }
                    ],
                },
            )
        elif path == "/props":
            self._send(200, {"modalities": {"vision": VISION}})
        else:
            self._send(404, {"error": f"no such path: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"_unparsed": raw}

        # Record the request, redacting the (huge) base64 image blob but keeping
        # its shape so the payload format stays verifiable.
        record = {"received_at": datetime.now().isoformat(timespec="seconds"), "body": parsed}
        try:
            for msg in record["body"].get("messages", []):
                content = msg.get("content")
                if isinstance(content, list):
                    for part in content:
                        url = (part.get("image_url") or {}).get("url") if isinstance(part, dict) else None
                        if isinstance(url, str) and len(url) > 120:
                            part["image_url"]["url"] = (
                                url[:60] + f"...<{len(url)} chars total>..."
                            )
        except Exception:  # noqa: BLE001 - logging must never break the response
            pass

        if self.path.split("?")[0] in ("/v1/chat/completions", "/chat/completions"):
            with LOCK:
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            self._send(
                200,
                {
                    "id": "mock-1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "mock-vision-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": CANNED},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1234,
                        "completion_tokens": 56,
                        "total_tokens": 1290,
                    },
                },
            )
        else:
            self._send(404, {"error": f"no such path: {self.path}"})


def main() -> None:
    global VISION
    p = argparse.ArgumentParser(description="Mock OpenAI-compatible server.")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--vision",
        choices=["yes", "no"],
        default="yes",
        help="whether this fake server claims image support",
    )
    a = p.parse_args()
    VISION = a.vision == "yes"

    server = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"mock server on http://{a.host}:{a.port}/v1   vision={a.vision}")
    print(f"requests logged to {LOG}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
