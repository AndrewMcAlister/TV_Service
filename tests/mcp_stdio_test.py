#!/usr/bin/env python3
"""
Protocol test for heatmap_mcp.py's stdio transport, plus the market-picture file.

Deliberately drives the server through *file handles* rather than pipes:
`subprocess` with stdin/stdout=PIPE opens named pipes, which restricted
environments (including the DSH sandbox) forbid. Redirecting files exercises
the exact same stdio code path with no pipes at all.

    python tests/mcp_stdio_test.py

Exit code 0 means every expectation held.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = ROOT / "heatmap_mcp.py"

LOG_DIR = ROOT / "logs"
PICTURE = LOG_DIR / "latest_market_picture_USA.txt"

REQUESTS: list[dict] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "file-handle-test", "version": "1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    {"jsonrpc": "2.0", "id": 5, "method": "resources/templates/list"},
    {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "probe_model_server", "arguments": {}},
    },
    {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "sector_performance", "arguments": {}},
    },
    # the market picture, deterministic path only
    {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "market_picture", "arguments": {}},
    },
    {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "no_such_tool", "arguments": {}},
    },
    {"jsonrpc": "2.0", "id": 10, "method": "no/such/method"},
]


def run_server(requests: list[dict], tag: str) -> tuple[list[dict], str]:
    """Run one server session over file handles. Returns (responses, stderr)."""
    req_path = HERE / f"mcp_{tag}_requests.jsonl"
    resp_path = HERE / f"mcp_{tag}_responses.jsonl"
    err_path = HERE / f"mcp_{tag}_stderr.log"

    req_path.write_text("\n".join(json.dumps(r) for r in requests) + "\n", encoding="utf-8")

    with (
        open(req_path, "r", encoding="utf-8") as fin,
        open(resp_path, "w", encoding="utf-8") as fout,
        open(err_path, "w", encoding="utf-8") as ferr,
    ):
        proc = subprocess.run(
            [sys.executable, str(SERVER)],
            stdin=fin,
            stdout=fout,
            stderr=ferr,
            timeout=400,
            check=False,
        )

    messages: list[dict] = []
    for line in resp_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                messages.append(json.loads(line))
            except ValueError:
                messages.append({"_raw": line})
    return messages, err_path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    failures: list[str] = []

    def expect(condition: bool, label: str) -> None:
        print(("  PASS  " if condition else "  FAIL  ") + label)
        if not condition:
            failures.append(label)

    # ---------------------------------------------------------------- phase 1
    print("=== phase 1: protocol suite ===")
    messages, stderr_text = run_server(REQUESTS, "main")
    by_id = {m["id"]: m for m in messages if isinstance(m.get("id"), int)}
    print(f"responses: {len(messages)} (expected 10)\n")

    result = by_id.get(1, {}).get("result", {})
    print("initialize:")
    expect(result.get("protocolVersion") == "2025-06-18", "protocolVersion echoed")
    expect(bool(result.get("serverInfo")), f"serverInfo: {result.get('serverInfo')}")
    expect(bool(result.get("instructions")), "publishes instructions")

    tools = by_id.get(2, {}).get("result", {}).get("tools", [])
    names = sorted(t["name"] for t in tools)
    print("\ntools/list:")
    expect(len(tools) == 6, f"6 tools advertised: {names}")
    expect("market_picture" in names, "market_picture is advertised")
    expect(all("inputSchema" in t for t in tools), "every tool has an inputSchema")
    market_schema = next((t for t in tools if t["name"] == "market_picture"), {})
    expect(
        "include_chart_read" in market_schema.get("inputSchema", {}).get("properties", {}),
        "market_picture exposes include_chart_read",
    )

    print("\nping / resources:")
    expect(by_id.get(3, {}).get("result") == {}, "ping returns an empty result")
    expect(isinstance(by_id.get(4, {}).get("result", {}).get("resources"), list), "resource list returned")
    expect(
        by_id.get(5, {}).get("result", {}).get("resourceTemplates") == [],
        "empty template list",
    )

    sectors = by_id.get(7, {}).get("result", {})
    expect(
        len((sectors.get("structuredContent") or {}).get("sectors", [])) == 11,
        "sector_performance returns 11 sectors",
    )

    # ------------------------------------------------------- market picture
    print("\ntools/call market_picture:")
    pic = by_id.get(8, {}).get("result", {})
    pic_text = (pic.get("content") or [{}])[0].get("text", "")
    pic_struct = pic.get("structuredContent") or {}
    print("       " + pic_text.replace("\n", "\n       ")[:900])

    expect(pic.get("isError") is False, "isError is false")
    expect("MARKET PICTURE - USA" in pic_text, "text is the market picture")
    for field in ("BREADTH", "LEADERS", "LAGGARDS", "DISPERSION", "TONE", "PROVENANCE"):
        expect(field in pic_text, f"summary contains {field}")
    expect(pic_struct.get("tone") is not None, f"tone: {pic_struct.get('tone')}")
    expect(pic_struct.get("character") is not None, f"character: {pic_struct.get('character')}")
    expect(len(pic_struct.get("leaders", [])) == 3, "three leaders in structuredContent")
    expect(pic_struct.get("chart_included") is False, "chart read omitted by default")

    print("\nmarket picture file:")
    expect(PICTURE.exists(), f"file written: {PICTURE}")
    if PICTURE.exists():
        written = PICTURE.read_text(encoding="utf-8")
        expect("MARKET PICTURE - USA" in written, "file holds the same summary")
        expect(written.strip() == pic_text.split("\n\nwritten to")[0].strip(),
               "file content matches the returned text")
        expect(pic_struct.get("file") == str(PICTURE), "structuredContent names the file")
        print(f"       {PICTURE.stat().st_size} bytes")

    # ------------------------------------------------------------- errors
    print("\nerror paths:")
    bad = by_id.get(9, {})
    expect("error" in bad, f"unknown tool is an error: {bad.get('error', {}).get('message')}")
    expect(
        by_id.get(10, {}).get("error", {}).get("code") == -32601,
        "unknown method is -32601",
    )

    lines = [json.dumps(m) for m in messages]
    print("\nstdout purity:")
    expect(all(m.get("jsonrpc") == "2.0" for m in messages),
           "every stdout line is a JSON-RPC message")
    print(f"       stderr carried {len(stderr_text.splitlines())} diagnostic line(s)")

    # ---------------------------------------------------------------- phase 2
    print("\n=== phase 2: the file is overwritten, not appended ===")
    sentinel = "STALE SENTINEL - should be gone after the next call\n"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PICTURE.write_text(sentinel, encoding="utf-8")
    print(f"       planted {len(sentinel)} bytes of sentinel into the file")

    messages2, _ = run_server(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "market_picture", "arguments": {}}},
        ],
        "overwrite",
    )
    by_id2 = {m["id"]: m for m in messages2 if isinstance(m.get("id"), int)}
    expect(by_id2.get(2, {}).get("result", {}).get("isError") is False, "second call succeeded")

    after = PICTURE.read_text(encoding="utf-8")
    expect("STALE SENTINEL" not in after, "sentinel is gone (file was overwritten)")
    expect("MARKET PICTURE - USA" in after, "file holds a fresh market picture")
    expect(len(messages2) == 2, f"two responses from the second session (got {len(messages2)})")

    print()
    if failures:
        print(f"FAILED: {len(failures)} expectation(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL EXPECTATIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
