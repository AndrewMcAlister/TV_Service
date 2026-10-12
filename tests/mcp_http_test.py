#!/usr/bin/env python3
"""
Protocol test for heatmap_mcp.py's Streamable HTTP transport.

Starts the server itself (stdout/stderr to files, never pipes), waits for it to
answer, exercises the endpoint over real HTTP, then shuts it down.

    python tests/mcp_http_test.py [--port 8099]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / "heatmap_mcp.py"


def post(url: str, payload: object, timeout: float = 120.0) -> tuple[int, object, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            headers = dict(resp.headers)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        headers = dict(exc.headers)
        status = exc.code
    try:
        return status, (json.loads(raw) if raw.strip() else None), headers
    except ValueError:
        return status, raw, headers


def post_raw(url: str, raw: bytes, timeout: float = 30.0) -> tuple[int, object]:
    """POST a literal body, so genuinely malformed JSON can be tested."""
    req = urllib.request.Request(
        url, data=raw, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}/mcp"
    out_path = HERE / "mcp_http_stdout.log"
    err_path = HERE / "mcp_http_stderr.log"

    failures: list[str] = []

    def expect(condition: bool, label: str) -> None:
        print(("  PASS  " if condition else "  FAIL  ") + label)
        if not condition:
            failures.append(label)

    with (
        open(out_path, "w", encoding="utf-8") as fout,
        open(err_path, "w", encoding="utf-8") as ferr,
    ):
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--http", "--port", str(args.port)],
            stdout=fout,
            stderr=ferr,
        )
        try:
            # wait for readiness
            ready = False
            for _ in range(40):
                time.sleep(0.5)
                try:
                    post(url, {"jsonrpc": "2.0", "id": 0, "method": "ping"}, timeout=5)
                    ready = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            print(f"server ready on {url}: {ready}\n")
            if not ready:
                print(err_path.read_text(encoding="utf-8", errors="replace")[-2000:])
                return 1

            print("initialize:")
            status, body, headers = post(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "http-test", "version": "1"},
                    },
                },
            )
            expect(status == 200, f"HTTP 200 (got {status})")
            expect(body.get("result", {}).get("protocolVersion") == "2025-06-18", "protocolVersion")
            expect("Mcp-Session-Id" in headers, f"Mcp-Session-Id header: {headers.get('Mcp-Session-Id')}")

            print("\nnotification (no id):")
            status, body, _ = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            expect(status == 202, f"HTTP 202 Accepted with empty body (got {status})")

            print("\ntools/list:")
            status, body, _ = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = body.get("result", {}).get("tools", [])
            expect(len(tools) == 6, f"6 tools (got {len(tools)}: {[t['name'] for t in tools]})")

            print("\ntools/call market_picture:")
            status, body, _ = post(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {"name": "market_picture", "arguments": {}},
                },
            )
            pic = body.get("result", {})
            pic_text = (pic.get("content") or [{}])[0].get("text", "")
            expect(status == 200 and pic.get("isError") is False, "call succeeded")
            expect("MARKET PICTURE - USA" in pic_text, "returns the market picture")
            expect((pic.get("structuredContent") or {}).get("file", "").endswith(
                "latest_market_picture_USA.txt"), "names the log file")

            print("\ntools/call sector_performance:")
            status, body, _ = post(
                url,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "sector_performance", "arguments": {}},
                },
            )
            result = body.get("result", {})
            sectors = (result.get("structuredContent") or {}).get("sectors", [])
            expect(status == 200 and result.get("isError") is False, "call succeeded")
            expect(len(sectors) == 11, f"11 sectors (got {len(sectors)})")

            print("\nresources/list:")
            status, body, _ = post(url, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
            expect(status == 200, "HTTP 200")
            expect(isinstance(body.get("result", {}).get("resources"), list), "resource list returned")

            print("\nbatch request:")
            status, body, _ = post(
                url,
                [
                    {"jsonrpc": "2.0", "id": 5, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 6, "method": "ping"},
                ],
            )
            expect(isinstance(body, list) and len(body) == 2, f"two responses (got {body if not isinstance(body, list) else len(body)})")

            print("\ntransport edges:")
            expect(get(url) == 405, "GET /mcp is 405 (no server-initiated stream)")
            expect(post(f"http://127.0.0.1:{args.port}/nope", {"jsonrpc": "2.0", "id": 9, "method": "ping"})[0] == 404,
                   "unknown path is 404")
            expect(post_raw(url, b"not json at all")[0] == 400, "malformed body is 400")
            expect(post_raw(url, b'"a JSON string, not a message"')[0] == 400,
                   "valid JSON that is not a message is 400")

            print()
            if failures:
                print(f"FAILED: {len(failures)} expectation(s): {failures}")
                return 1
            print("ALL HTTP EXPECTATIONS PASSED")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
