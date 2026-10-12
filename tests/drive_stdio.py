#!/usr/bin/env python3
"""
Spawn an MCP stdio server through *file handles* and run a fixed conversation.

This exists to verify the exact command line the harness will use, without the
pipes `subprocess` would normally open (which restricted environments forbid).

    python tests/drive_stdio.py --interpreter <python.exe> --server <heatmap_mcp.py>

Exit code 0 means the server completed the conversation and every stdout line
was a JSON-RPC message.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

CONVERSATION = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "harness-simulation", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "market_picture", "arguments": {}},
    },
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interpreter", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    if not Path(args.interpreter).exists():
        print(f"interpreter not found: {args.interpreter}")
        return 1
    if not Path(args.server).exists():
        print(f"server not found: {args.server}")
        return 1

    print("spawning:")
    print(f"  {args.interpreter}")
    print(f"  {args.server}")

    # Scratch files live beside this script: some environments lock down the
    # platform temp directory, which `tempfile` would otherwise use.
    req = HERE / "drive_req.jsonl"
    resp = HERE / "drive_resp.jsonl"
    err = HERE / "drive_err.log"
    req.write_text(
        "\n".join(json.dumps(m) for m in CONVERSATION) + "\n", encoding="utf-8"
    )

    with (
        open(req, "r", encoding="utf-8") as fin,
        open(resp, "w", encoding="utf-8") as fout,
        open(err, "w", encoding="utf-8") as ferr,
    ):
        proc = subprocess.run(
            [args.interpreter, args.server],
            stdin=fin,
            stdout=fout,
            stderr=ferr,
            timeout=args.timeout,
            check=False,
        )

    lines = [ln for ln in resp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    stderr_text = err.read_text(encoding="utf-8", errors="replace")

    print(f"exit code: {proc.returncode}")
    print(f"stdout lines: {len(lines)} (expected 3: two results plus one for id 3)")

    failures = 0
    impure = [ln for ln in lines if not ln.lstrip().startswith("{")]
    if impure:
        failures += 1
        print(f"  FAIL  stdout has non-JSON output: {impure[:2]}")

    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            failures += 1
            continue
        rid = msg.get("id")
        if rid == 1:
            info = msg.get("result", {}).get("serverInfo", {})
            print(f"  initialize     -> {info.get('name')} v{info.get('version')}")
        elif rid == 2:
            names = [t["name"] for t in msg.get("result", {}).get("tools", [])]
            print(f"  tools/list     -> {len(names)} tools: {', '.join(sorted(names))}")
            if "market_picture" not in names:
                failures += 1
                print("  FAIL  market_picture missing")
        elif rid == 3:
            res = msg.get("result", {})
            ok = res.get("isError") is False
            print(f"  market_picture -> isError={res.get('isError')}")
            if not ok:
                failures += 1
            text = (res.get("content") or [{}])[0].get("text", "")
            for ln in text.splitlines():
                print(f"      | {ln}")
            if "MARKET PICTURE - USA" not in text:
                failures += 1
                print("  FAIL  not a market picture")

    if stderr_text.strip():
        print(f"  (stderr carried {len(stderr_text.splitlines())} diagnostic lines)")

    print()
    if failures:
        print(f"FAILED ({failures} problem(s))")
        return 1
    print("PASSED - the registered command completes a full MCP conversation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
