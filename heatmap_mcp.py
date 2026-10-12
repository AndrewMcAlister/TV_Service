#!/usr/bin/env python3
"""
MCP server exposing the TradingView heat-map pipeline as tools.

Transports
    stdio (default)   for local MCP clients: Claude Desktop, DSH, Cursor...
    streamable HTTP   --http --port 8099, for anything that speaks HTTP

Standard library only -- no `mcp` package, no install step.

    python heatmap_mcp.py                 # stdio
    python heatmap_mcp.py --http          # http://127.0.0.1:8099/mcp

WHY THIS FILE GUARDS STDOUT SO CAREFULLY
    On the stdio transport, stdout *is* the JSON-RPC channel. The pipeline
    prints progress through Config.say(), and one stray line on stdout makes
    the client reject the stream as malformed. Every tool call therefore runs
    with stdout temporarily pointed at stderr; the protocol writer holds a
    reference to the real stdout.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import heatmap_report as hr  # noqa: E402  (the pipeline this server exposes)

SERVER_INFO = {"name": "tv-heatmap", "version": "1.0.0"}

# Newest first. Initialization echoes the client's version when we support it.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL = PROTOCOL_VERSIONS[0]

CAPABILITIES = {
    "tools": {"listChanged": False},
    "resources": {"subscribe": False, "listChanged": False},
}

INSTRUCTIONS = (
    "TradingView heat-map analysis backed by a local vision model.\n"
    "\n"
    "Reports contain four sections, and the difference between them matters:\n"
    "  2. VISION MODEL READ      a language model describing a picture. Its\n"
    "                            magnitudes are reliable but it confuses red\n"
    "                            with green on small tiles, and it has\n"
    "                            misread tickers entirely.\n"
    "  3. VERIFIED MARKET DATA   arithmetic on real closes. Exact.\n"
    "  4. FACT-CHECK             section 2 audited against real closes.\n"
    "\n"
    "Prefer sections 3 and 4 for any factual or directional claim. Treat\n"
    "section 2 as a lead to investigate, never as evidence.\n"
    "\n"
    "'Last session' means the most recent *completed* session: before the US\n"
    "open, or at a weekend, that is the previous trading day, not today."
)

REPORTS_DIR = HERE / "reports"

# The market picture is dumped here, overwriting the previous one every time.
# Override the location with TV_HEATMAP_LOGS when the default is not writable.
LOGS_DIR = Path(os.environ.get("TV_HEATMAP_LOGS") or (HERE / "logs"))
MARKET_PICTURE_FILENAME = "latest_market_picture_USA.txt"

# A deliberately short prompt: this reading lands in a one-screen summary, so a
# sprawling answer is worse than a terse one.
PICTURE_PROMPT = """You are reading a screenshot of a stock-market heat map.

In at most 6 short lines, say only what is clearly legible:
- the 2-3 sectors that look strongest, with their dominant tickers
- the 2-3 sectors that look weakest, with their dominant tickers

Rules: no preamble, no headings, no markdown, one sector per line. If a tile or
number is too small to read, say "unreadable" instead of guessing. Do not invent
tickers or percentages.
"""

# The protocol writer keeps the real stdout, because sys.stdout gets redirected
# to stderr for the duration of every tool call.
_OUT = sys.stdout


def _log(message: str) -> None:
    """Diagnostics go to stderr, which is always safe."""
    print(message, file=sys.stderr, flush=True)


@contextlib.contextmanager
def _protect_protocol_stream():
    """Send anything the pipeline prints to stderr, never to the wire."""
    real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = real


# --------------------------------------------------------------------------
# tool implementations
# --------------------------------------------------------------------------


def _cfg_from_args(args: dict[str, Any]) -> hr.Config:
    """Build a Config from tool arguments, ignoring anything unrecognised."""
    cfg = hr.Config()
    cfg.quiet = True  # belt and braces: say() prints nothing at all
    for key, value in (args or {}).items():
        if key in ("image", "out") and value:
            value = Path(value)
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg


def _run_pipeline(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _cfg_from_args(args)
    stamp = hr.datetime.now().strftime("%Y%m%d-%H%M%S")

    shot = cfg.image
    if not cfg.skip_capture and shot is None:
        shot = REPORTS_DIR / f"heatmap-{stamp}.png"
        shot = hr.capture(cfg, shot)
    elif shot is not None and not Path(shot).exists():
        raise FileNotFoundError(f"image does not exist: {shot}")

    answer = reasoning = ""
    if not cfg.skip_vision and shot is not None:
        answer, reasoning = hr.ask_vision(cfg, shot)

    rows: list[dict[str, Any]] = []
    data_error: str | None = None
    if not cfg.skip_market_data:
        rows, data_error = hr.sector_data(cfg)

    claims: list[dict[str, Any]] = []
    if not cfg.skip_factcheck and answer:
        claims = hr.factcheck_claims(cfg, answer)

    report = hr.build_report(
        cfg,
        shot=shot,
        answer=answer,
        reasoning=reasoning,
        rows=rows,
        data_error=data_error,
        claims=claims,
    )

    out = cfg.out or (REPORTS_DIR / f"heatmap-report-{stamp}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    flips = sum(1 for c in claims if c.get("sign_flip"))
    return {
        "report": report,
        "report_path": str(out),
        "screenshot_path": str(shot) if shot else None,
        "model": cfg.model,
        "session": sorted({r["date"] for r in rows}) or None,
        "claims_checked": sum(1 for c in claims if c.get("actual") is not None),
        "claims_with_wrong_sign": flips,
    }


def _tool_heatmap_report(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    result = _run_pipeline(args)
    text = result["report"]
    summary = []
    if result.get("report_path"):
        summary.append(f"Report written to {result['report_path']}")
    if result.get("screenshot_path"):
        summary.append(f"Screenshot: {result['screenshot_path']}")
    if result.get("session"):
        summary.append(f"Session covered: {', '.join(result['session'])}")
    if result.get("claims_checked"):
        summary.append(
            f"Fact-check: {result['claims_with_wrong_sign']} of "
            f"{result['claims_checked']} claimed figures had the wrong sign."
        )
    if summary:
        text = text + "\n" + "\n".join(summary) + "\n"
    return text, result


def _tool_capture_heatmap(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    cfg = _cfg_from_args(args)
    stamp = hr.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = cfg.out or (REPORTS_DIR / f"heatmap-{stamp}.png")
    if dest.suffix.lower() != ".png":
        dest = dest.with_suffix(".png")
    shot = hr.capture(cfg, dest)
    size_kb = shot.stat().st_size / 1024
    return (
        f"Captured {cfg.url}\nSaved: {shot} ({size_kb:.0f} KB)\n"
        f"Viewport {cfg.width}x{cfg.height} at {cfg.scale}x.",
        {"screenshot_path": str(shot), "url": cfg.url, "size_kb": round(size_kb, 1)},
    )


def _tool_sector_performance(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    cfg = _cfg_from_args(args)
    rows, error = hr.sector_data(cfg)
    if not rows:
        raise RuntimeError(f"no sector data available ({error or 'unknown error'})")

    ranked = sorted(rows, key=lambda r: (r["day"] is None, -(r["day"] or 0)))
    dates = sorted({r["date"] for r in rows})
    lines = [
        "SPDR sector ETF performance (deterministic; no model involved)",
        f"session(s): {', '.join(dates)}",
        "",
        f"{'#':>2}  {'sector':<24}{'last':>9}{'5-day':>9}{'1-month':>9}",
        "-" * 60,
    ]
    for i, r in enumerate(ranked, 1):
        lines.append(
            f"{i:>2}  {r['sector']:<24}{hr._fmt_pct(r['day']):>9}"
            f"{hr._fmt_pct(r['five_day']):>9}{hr._fmt_pct(r['month']):>9}"
        )
    lines += ["", "ETF proxies, cap-weighted within each sector."]
    if error:
        lines.append(f"partial: {error}")

    structured = {
        "session": dates,
        "sectors": [
            {
                "symbol": r["symbol"],
                "sector": r["sector"],
                "close": r["close"],
                "change_pct": None if r["day"] is None else round(r["day"], 4),
                "five_day_pct": None if r["five_day"] is None else round(r["five_day"], 4),
                "month_pct": None if r["month"] is None else round(r["month"], 4),
            }
            for r in ranked
        ],
    }
    return "\n".join(lines), structured


def _tool_analyze_image(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    image = args.get("image")
    if not image:
        raise ValueError("'image' is required: a path to a PNG/JPEG screenshot")
    path = Path(image)
    if not path.exists():
        raise FileNotFoundError(f"image does not exist: {path}")

    cfg = _cfg_from_args({**args, "skip_capture": True})
    answer, reasoning = hr.ask_vision(cfg, path)

    claims: list[dict[str, Any]] = []
    if not cfg.skip_factcheck and answer:
        claims = hr.factcheck_claims(cfg, answer)

    lines = [
        "VISION MODEL READ (unverified: a model describing a picture)",
        "=" * 62,
        answer or "(the model returned no text)",
    ]
    if claims:
        flips = sum(1 for c in claims if c.get("sign_flip"))
        lines += ["", "FACT-CHECK AGAINST REAL CLOSES", "=" * 62,
                  f"{'ticker':<9}{'claimed':>10}{'actual':>10}"]
        for c in claims:
            actual = "n/a" if c["actual"] is None else hr._fmt_pct(c["actual"])
            flag = ""
            if c.get("sign_flip"):
                flag = "  <-- wrong sign"
            lines.append(f"{c['symbol']:<9}{hr._fmt_pct(c['claimed']):>10}{actual:>10}{flag}")
        lines += ["", f"{flips} of {len(claims)} claims had the wrong sign."]
    if reasoning and cfg.include_reasoning:
        lines += ["", "MODEL REASONING", "=" * 62, reasoning]

    return "\n".join(lines), {
        "answer": answer,
        "image": str(path),
        "claims": claims,
        "reasoning": reasoning if cfg.include_reasoning else None,
    }


def _tool_probe_server(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    cfg = _cfg_from_args(args)
    lines: list[str] = []
    buffer: list[str] = []

    # hr.probe() prints; capture its output by pointing stdout at a list.
    import io

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        cfg.quiet = False
        code = hr.probe(cfg)
    lines.append(sink.getvalue().rstrip())

    model = hr.resolve_model(cfg)
    vision = hr.vision_capability(cfg)
    lines += [
        "",
        f"model id        : {model}",
        f"vision (images) : {'YES' if vision else 'NO' if vision is False else 'unknown'}",
    ]
    if vision is False:
        lines.append(
            "  -> the server was started without image support; vision tools will "
            "refuse until it is restarted with it (Strata: --vision yes)"
        )
    return "\n".join(lines), {
        "reachable": code == 0,
        "base": cfg.base,
        "model": model,
        "vision": vision,
        "probe_output": sink.getvalue(),
    }


def _tool_market_picture(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The one-screen summary, and the same text written to the logs folder."""
    cfg = _cfg_from_args(args)
    cfg.quiet = True
    include_chart = bool(args.get("include_chart_read"))

    rows, error = hr.sector_data(cfg)
    if not rows:
        raise RuntimeError(f"no sector data available ({error or 'unknown error'})")

    chart_read: str | None = None
    claims: list[dict[str, Any]] = []

    if include_chart:
        stamp = hr.datetime.now().strftime("%Y%m%d-%H%M%S")
        image = args.get("image")
        if image:
            shot = Path(image)
            if not shot.exists():
                raise FileNotFoundError(f"image does not exist: {shot}")
        else:
            shot = hr.capture(cfg, REPORTS_DIR / f"heatmap-{stamp}.png")
        chart_read, _ = hr.ask_vision(cfg, shot, PICTURE_PROMPT)
        if chart_read and not cfg.skip_factcheck:
            claims = hr.factcheck_claims(cfg, chart_read)

    text = hr.build_market_picture(rows, chart_read=chart_read, chart_claims=claims)

    # Overwrite the previous picture every time: this is a "latest state" file.
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    target = LOGS_DIR / MARKET_PICTURE_FILENAME
    target.write_text(text, encoding="utf-8")

    facts = hr.market_picture_facts(rows)
    structured: dict[str, Any] = {
        "file": str(target),
        "written_bytes": target.stat().st_size,
        "session_dates": facts["session_dates"],
        "tone": facts["tone"],
        "character": facts["character"],
        "breadth": {
            "advancing": facts["advancing"],
            "declining": facts["declining"],
            "unchanged": facts["unchanged"],
            "counted": facts["sectors_counted"],
        },
        "leaders": facts["leaders"],
        "laggards": facts["laggards"],
        "dispersion_pp": facts["dispersion_pp"],
        "chart_included": chart_read is not None,
        "chart_claims_wrong_sign": sum(1 for c in claims if c.get("sign_flip")),
    }

    return text + f"\n\nwritten to {target} (overwrote the previous one)\n", structured


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]] = {
    "market_picture": _tool_market_picture,
    "heatmap_report": _tool_heatmap_report,
    "capture_heatmap": _tool_capture_heatmap,
    "sector_performance": _tool_sector_performance,
    "analyze_heatmap_image": _tool_analyze_image,
    "probe_model_server": _tool_probe_server,
}

_ENGINE = ["auto", "playwright", "edge"]
_EFFORT = ["none", "low", "medium", "high", "off"]

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "market_picture",
        "title": "Market picture (summary)",
        "description": (
            "The one-screen US market picture: breadth, leaders, laggards, "
            "dispersion, tone and leadership character, built from verified sector "
            "closes. Takes about 8 seconds because it uses no browser and no model. "
            "The identical text is written to "
            f"{MARKET_PICTURE_FILENAME} in the logs folder, overwriting the previous "
            "copy each time. Set include_chart_read to also append a vision model's "
            "reading of the heat-map chart, fact-checked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_chart_read": {
                    "type": "boolean",
                    "description": "Also capture the chart, read it with the vision model, and fact-check. Adds ~25s.",
                },
                "image": {
                    "type": "string",
                    "description": "With include_chart_read: read this screenshot instead of capturing one.",
                },
                "effort": {"type": "string", "enum": _EFFORT, "description": "Model reasoning effort."},
                "skip_factcheck": {"type": "boolean"},
                "base": {"type": "string", "description": "OpenAI-compatible base URL of the model server."},
                "model": {"type": "string", "description": "Model id; auto-detected when omitted."},
                "engine": {"type": "string", "enum": _ENGINE, "description": "Capture backend for the chart read."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
    {
        "name": "heatmap_report",
        "title": "Full heat-map report",
        "description": (
            "Capture a TradingView heat-map page, read it with the local vision "
            "model, cross-check sector performance against real closes, fact-check "
            "the model's own numbers, and write a text report. Returns the whole "
            "report. Takes roughly 30 seconds: it drives a browser and a local LLM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Heat-map page to capture."},
                "image": {"type": "string", "description": "Use this screenshot instead of capturing."},
                "out": {"type": "string", "description": "Where to write the report."},
                "engine": {"type": "string", "enum": _ENGINE, "description": "'edge' needs no Python packages."},
                "wait": {"type": "number", "description": "Seconds to let the tiles render."},
                "scale": {"type": "number", "description": "Device scale factor; higher reads better."},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "effort": {"type": "string", "enum": _EFFORT, "description": "Model reasoning effort."},
                "skip_capture": {"type": "boolean"},
                "skip_vision": {"type": "boolean"},
                "skip_market_data": {"type": "boolean"},
                "skip_factcheck": {"type": "boolean"},
                "force_vision": {"type": "boolean", "description": "Send the image even if the server reports no vision."},
                "include_reasoning": {"type": "boolean"},
                "base": {"type": "string", "description": "OpenAI-compatible base URL of the model server."},
                "model": {"type": "string", "description": "Model id; auto-detected when omitted."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
    {
        "name": "sector_performance",
        "title": "Sector performance (no model)",
        "description": (
            "Rank the 11 SPDR sector ETFs by their most recent completed session, "
            "with 5-day and 1-month context. Pure arithmetic on published closes: "
            "no browser, no model, nothing to hallucinate. Use this when you want "
            "the numbers rather than a description of a picture."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "analyze_heatmap_image",
        "title": "Read an existing screenshot",
        "description": (
            "Run only the vision and fact-check stages on a screenshot you already "
            "have. No browser involved. Returns the model's reading plus every "
            "ticker percentage it claimed, checked against real closes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "Path to a PNG/JPEG screenshot."},
                "effort": {"type": "string", "enum": _EFFORT},
                "skip_factcheck": {"type": "boolean"},
                "include_reasoning": {"type": "boolean"},
                "base": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["image"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "capture_heatmap",
        "title": "Screenshot only",
        "description": "Screenshot a heat-map page and save it. No model call. Returns the image path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "out": {"type": "string", "description": "Destination path (a .png)."},
                "engine": {"type": "string", "enum": _ENGINE},
                "wait": {"type": "number"},
                "scale": {"type": "number"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    },
    {
        "name": "probe_model_server",
        "title": "Check the model server",
        "description": (
            "Report whether the local model server is reachable, which model id it "
            "serves, and whether image input is enabled. Call this first when a "
            "vision request fails."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"base": {"type": "string", "description": "Base URL, e.g. http://127.0.0.1:8080/v1"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]


# --------------------------------------------------------------------------
# resources: the reports directory
# --------------------------------------------------------------------------


def _resource_list() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not REPORTS_DIR.is_dir():
        return items
    for path in sorted(REPORTS_DIR.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".txt", ".png"):
            continue
        is_text = path.suffix.lower() == ".txt"
        items.append(
            {
                "uri": f"heatmap://reports/{path.name}",
                "name": path.name,
                "title": path.name,
                "description": (
                    "A generated heat-map report."
                    if is_text
                    else "A captured heat-map screenshot."
                ),
                "mimeType": "text/plain" if is_text else "image/png",
            }
        )
    return items


def _resource_read(uri: str) -> dict[str, Any]:
    prefix = "heatmap://reports/"
    if not uri.startswith(prefix):
        raise ValueError(f"unknown resource uri: {uri}")
    name = uri[len(prefix):]
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid resource name")
    path = REPORTS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"no such resource: {uri}")

    if path.suffix.lower() == ".png":
        blob = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"contents": [{"uri": uri, "mimeType": "image/png", "blob": blob}]}
    return {
        "contents": [
            {"uri": uri, "mimeType": "text/plain", "text": path.read_text(encoding="utf-8", errors="replace")}
        ]
    }


# --------------------------------------------------------------------------
# JSON-RPC dispatch
# --------------------------------------------------------------------------


def _ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    if not isinstance(request, dict) or "method" not in request:
        return _err(request.get("id") if isinstance(request, dict) else None,
                    -32600, "Invalid Request")

    method = request.get("method")
    rid = request.get("id")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        requested = (params.get("protocolVersion") or "").strip()
        agreed = requested if requested in PROTOCOL_VERSIONS else PREFERRED_PROTOCOL
        _log(f"initialize: client={params.get('clientInfo')} requested={requested!r} -> {agreed}")
        return _ok(rid, {
            "protocolVersion": agreed,
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _ok(rid, {})

    if method == "tools/list":
        return _ok(rid, {"tools": TOOL_DEFS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _err(rid, -32602, f"unknown tool: {name}")
        _log(f"tools/call {name} {json.dumps(arguments)[:200]}")
        try:
            with _protect_protocol_stream():
                text, structured = handler(arguments)
        except Exception as exc:  # noqa: BLE001 - reported to the client, not raised
            _log("tool failed:\n" + traceback.format_exc())
            return _ok(rid, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        }
        if structured:
            result["structuredContent"] = structured
        return _ok(rid, result)

    if method == "resources/list":
        return _ok(rid, {"resources": _resource_list()})

    if method == "resources/read":
        uri = params.get("uri")
        try:
            return _ok(rid, _resource_read(uri))
        except Exception as exc:  # noqa: BLE001
            return _err(rid, -32602, f"{type(exc).__name__}: {exc}")

    if method == "resources/templates/list":
        return _ok(rid, {"resourceTemplates": []})

    if is_notification:
        _log(f"ignoring unknown notification: {method}")
        return None
    return _err(rid, -32601, f"method not found: {method}")


# --------------------------------------------------------------------------
# stdio transport
# --------------------------------------------------------------------------


def serve_stdio() -> int:
    _log(f"tv-heatmap MCP server on stdio (pid {os.getpid()})")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            _OUT.write(json.dumps(_err(None, -32700, f"parse error: {exc}")) + "\n")
            _OUT.flush()
            continue

        response = handle(request)
        if response is not None:
            _OUT.write(json.dumps(response) + "\n")
            _OUT.flush()
    return 0


# --------------------------------------------------------------------------
# streamable HTTP transport
# --------------------------------------------------------------------------


def serve_http(host: str, port: int) -> int:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    session_id = "tv-heatmap"

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tv-heatmap-mcp/1.0"

        def log_message(self, fmt, *args):
            _log("http " + (fmt % args))

        def _send(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Mcp-Session-Id", session_id)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            # No server-initiated stream on this endpoint.
            self._send(405, {"error": "use POST"}, {"Allow": "POST"})

        def do_DELETE(self) -> None:  # noqa: N802
            self._send(200, None)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path not in ("/mcp", "/"):
                self._send(404, {"error": f"no such endpoint: {path}"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                request = json.loads(raw)
            except ValueError as exc:
                self._send(400, _err(None, -32700, f"parse error: {exc}"))
                return

            if not isinstance(request, (dict, list)):
                # Valid JSON, but not a JSON-RPC message.
                self._send(400, _err(None, -32600, "Invalid Request: expected an object or array"))
                return

            if isinstance(request, list):  # batched
                responses = [r for r in (handle(item) for item in request) if r is not None]
                self._send(200, responses if responses else None)
                return

            response = handle(request)
            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.send_header("Mcp-Session-Id", session_id)
                self.end_headers()
                return
            self._send(200, response)

    server = ThreadingHTTPServer((host, port), Handler)
    _log(f"tv-heatmap MCP server on http://{host}:{port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("stopping")
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MCP server for the TradingView heat-map pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--list-tools", action="store_true", help="print the tool catalogue and exit")
    args = parser.parse_args(argv)

    if args.list_tools:
        for tool in TOOL_DEFS:
            print(f"{tool['name']:<24} {tool['title']}")
        return 0

    if args.http:
        return serve_http(args.host, args.port)
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
