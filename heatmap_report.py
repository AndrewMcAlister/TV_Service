#!/usr/bin/env python3
"""
TradingView heat map -> local vision LLM -> text report.

Pipeline
    1. CAPTURE   Screenshot a TradingView heat-map page with Playwright
                 (or reuse an existing image with --image).
    2. VISION    Send that screenshot to a local vision model over the
                 OpenAI-compatible API (Strata, Ollama, LM Studio, vLLM...).
    3. VERIFY    Independently fetch sector ETF performance -- no model involved.
    4. REPORT    Write everything to a .txt file.

Only stage 1 needs a third-party package (playwright). Stages 2-4 use the
Python standard library alone.

Examples
    # What is my local model server actually offering?
    python heatmap_report.py --probe

    # Full run against Strata, report written to reports/
    python heatmap_report.py --vision yes

    # Analyse a screenshot you already have (no browser needed)
    python heatmap_report.py --image shot.png

    # No model / no browser: just the deterministic sector numbers
    python heatmap_report.py --skip-capture --skip-vision
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://www.tradingview.com/heatmap/stock/"
DEFAULT_BASE = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "strata"  # fallback only; the real id is read from /v1/models

# The 11 SPDR sector ETFs: a deterministic, cap-weighted read of each sector.
SECTOR_ETFS: dict[str, str] = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

VISION_PROMPT = """You are reading a screenshot of a stock-market heat map.

Report ONLY what is actually legible in the image.
- Group your answer by sector.
- For each sector say whether it looks strongly up, mildly up, flat, mildly
  down, or strongly down, and name the tickers that dominate it.
- If a tile, label or number is too small or blurry to read, write
  "unreadable" for it. DO NOT guess or invent tickers or percentages.
- If the image does not look like a heat map at all, say so plainly.

Answer in exactly this shape:

STRONGEST SECTORS: ...
WEAKEST SECTORS: ...
NOTABLE TICKERS: ...
CONFIDENCE: high|medium|low - one sentence saying why
UNREADABLE: ...
"""

USER_AGENT = "Mozilla/5.0 (compatible; heatmap-report/1.0)"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


@dataclass
class Config:
    url: str = DEFAULT_URL
    image: Path | None = None
    out: Path | None = None
    base: str = DEFAULT_BASE
    model: str | None = None
    api_key: str | None = None
    # capture
    engine: str = "auto"
    browser: str | None = None
    width: int = 1920
    height: int = 1080
    scale: float = 2.0
    wait: float = 8.0
    headed: bool = False
    full_page: bool = False
    timeout: float = 60.0
    # behaviour
    skip_capture: bool = False
    skip_vision: bool = False
    skip_market_data: bool = False
    effort: str = "none"
    max_tokens: int = 1600
    include_reasoning: bool = False
    force_vision: bool = False
    skip_factcheck: bool = False
    probe: bool = False
    quiet: bool = False

    def say(self, msg: str = "") -> None:
        if not self.quiet:
            print(msg, flush=True)

    @property
    def root(self) -> str:
        """Server root, i.e. the base URL without a trailing /v1."""
        b = self.base.rstrip("/")
        return b[: -len("/v1")] if b.endswith("/v1") else b


# --------------------------------------------------------------------------
# tiny HTTP helper (standard library only)
# --------------------------------------------------------------------------


def http_json(
    url: str,
    payload: Any | None = None,
    *,
    timeout: float = 60.0,
    token: str | None = None,
    method: str | None = None,
) -> tuple[int, Any]:
    """Request a URL, return (status, parsed_json_or_text). Never raises on HTTP."""
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, _maybe_json(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, _maybe_json(body)
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the caller
        return 0, f"{type(exc).__name__}: {exc}"


def _maybe_json(body: str) -> Any:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body


def _short(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------
# stage 0: probe the model server
# --------------------------------------------------------------------------


def probe(cfg: Config) -> int:
    """Report what the local model server exposes. Returns a process exit code."""
    cfg.say(f"Probing {cfg.root}\n")
    ok = False

    for name, url in (
        ("GET /health", f"{cfg.root}/health"),
        ("GET /v1/models", f"{cfg.base.rstrip('/')}/models"),
        ("GET /props", f"{cfg.root}/props"),
    ):
        status, body = http_json(url, timeout=15.0, token=cfg.api_key)
        flag = "OK " if status == 200 else "!! "
        cfg.say(f"{flag}{name} -> {status}")
        if status == 200:
            ok = True
            # Surface input modalities, which is what vision depends on.
            if isinstance(body, dict) and "data" in body:
                for entry in body.get("data", []):
                    if isinstance(entry, dict):
                        mods = entry.get("input_modalities") or entry.get("modalities")
                        cfg.say(f"      model id : {entry.get('id')}")
                        if mods:
                            cfg.say(f"      modalities: {mods}")
            elif isinstance(body, dict) and "chat_template_caps" in body:
                cfg.say(f"      engine/props keys: {sorted(body)[:8]}")
        else:
            cfg.say(f"      {_short(body, 200)}")
        cfg.say()

    capability = vision_capability(cfg)
    label = {True: "YES", False: "NO", None: "unknown"}[capability]
    cfg.say(f"Vision (image input): {label}")
    if capability is False:
        cfg.say("  -> the server started without image support; the model cannot")
        cfg.say("     see screenshots until it is restarted with it (Strata:")
        cfg.say("     --vision yes). The vision stage will refuse to run.")

    if not ok:
        cfg.say(
            "Nothing answered. Is Strata (or your other server) running, and is\n"
            f"the base URL right? Currently: {cfg.base}"
        )
    return 0 if ok else 1


# --------------------------------------------------------------------------
# stage 1: capture
# --------------------------------------------------------------------------


def capture_playwright(cfg: Config, dest: Path) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright is not installed (only needed for capture).\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
            "Or pass --image <file> to analyse a screenshot you already have."
        ) from None

    dest.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not cfg.headed)
        context = browser.new_context(
            viewport={"width": cfg.width, "height": cfg.height},
            device_scale_factor=cfg.scale,
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        cfg.say(f"Loading {cfg.url} ({cfg.width}x{cfg.height} @ {cfg.scale}x)")
        page.goto(cfg.url, wait_until="domcontentloaded", timeout=int(cfg.timeout * 1000))

        # Best-effort dismissal of consent banners. Not fatal if none appear.
        for selector in (
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            "#onetrust-accept-btn-handler",
            '[data-name="accept-cookies"]',
        ):
            try:
                page.locator(selector).first.click(timeout=1500)
                cfg.say(f"  dismissed consent banner via {selector}")
                break
            except Exception:  # noqa: BLE001 - absent banner is expected
                continue

        try:
            page.wait_for_load_state("networkidle", timeout=int(cfg.wait * 1000))
        except Exception:  # noqa: BLE001 - heat maps often poll forever
            cfg.say("  network never went idle; continuing after the wait")

        cfg.say(f"  waiting {cfg.wait}s for tiles to render")
        time.sleep(cfg.wait)
        page.screenshot(path=str(dest), full_page=cfg.full_page)
        context.close()
        browser.close()

    size_kb = dest.stat().st_size / 1024
    cfg.say(f"Saved screenshot: {dest} ({size_kb:.0f} KB)")
    return dest


BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser() -> str | None:
    """Path to a system Chrome/Edge, if one is installed."""
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def capture_edge(cfg: Config, dest: Path) -> Path:
    """Screenshot with a system Chrome/Edge, using its own --screenshot flag.

    Needs no Python package at all. It also opens no pipes: the browser writes
    the image straight to a file, and stdio goes to the null device. That makes
    it the backend that works in restricted environments where spawning a
    child with piped stdio is forbidden.
    """
    exe = cfg.browser or find_browser()
    if not exe:
        raise SystemExit(
            "No system Chrome or Edge found for --engine edge.\n"
            "Install one, pass --browser <path>, or use --engine playwright."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    profile = dest.parent.parent / ".browser-profile"

    args = [
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        f"--window-size={cfg.width},{cfg.height}",
        f"--force-device-scale-factor={cfg.scale}",
        f"--virtual-time-budget={int(cfg.wait * 1000)}",
        f"--screenshot={dest}",
        cfg.url,
    ]

    cfg.say(f"Launching {Path(exe).name} headless ({cfg.width}x{cfg.height} @ {cfg.scale}x)")
    proc = subprocess.run(
        [exe, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=cfg.timeout * 3,
        check=False,
    )
    cfg.say(f"  browser exited with code {proc.returncode}")

    if not dest.exists():
        raise SystemExit(
            "The browser produced no screenshot.\n"
            "  - raise the wait:  --wait 25\n"
            "  - watch it run:    --headed\n"
            "  - some environments forbid headless browsers entirely; use --image\n"
            "    with a screenshot you took yourself."
        )
    cfg.say(f"Saved screenshot: {dest} ({dest.stat().st_size / 1024:.0f} KB)")
    return dest


def _has_playwright() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def capture(cfg: Config, dest: Path) -> Path:
    """Choose a capture backend."""
    engine = cfg.engine
    if engine == "auto":
        engine = "playwright" if _has_playwright() else "edge"
        cfg.say(f"Auto-selected capture engine: {engine}")
    if engine == "edge":
        return capture_edge(cfg, dest)
    return capture_playwright(cfg, dest)


# --------------------------------------------------------------------------
# stage 2: vision
# --------------------------------------------------------------------------


def resolve_model(cfg: Config) -> str:
    """Return the model id to request, asking the server when not pinned.

    Servers name themselves: Strata reports e.g. 'qwen3.8-flash-next-iq3_s',
    not a generic 'strata', so guessing the id is a common failure.
    """
    if cfg.model:
        return cfg.model
    status, body = http_json(
        f"{cfg.base.rstrip('/')}/models", timeout=15.0, token=cfg.api_key
    )
    if status == 200 and isinstance(body, dict):
        for entry in body.get("data") or []:
            if isinstance(entry, dict) and entry.get("id"):
                return str(entry["id"])
    return DEFAULT_MODEL


def vision_capability(cfg: Config) -> bool | None:
    """True / False / None (unknown): does this server accept image input?"""
    status, body = http_json(f"{cfg.root}/health", timeout=15.0, token=cfg.api_key)
    if status == 200 and isinstance(body, dict) and isinstance(body.get("images"), bool):
        return body["images"]

    status, body = http_json(f"{cfg.root}/props", timeout=15.0, token=cfg.api_key)
    if status == 200 and isinstance(body, dict):
        vision = (body.get("modalities") or {}).get("vision")
        if isinstance(vision, bool):
            return vision

    status, body = http_json(
        f"{cfg.base.rstrip('/')}/models", timeout=15.0, token=cfg.api_key
    )
    if status == 200 and isinstance(body, dict):
        for entry in body.get("data") or []:
            mods = ((entry or {}).get("architecture") or {}).get("input_modalities")
            if isinstance(mods, list):
                return any("image" in str(m).lower() for m in mods)
    return None


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def ask_vision(cfg: Config, image: Path, prompt: str = VISION_PROMPT) -> tuple[str, str]:
    """Return (answer, reasoning). Either may be empty."""
    model = resolve_model(cfg)
    capability = vision_capability(cfg)
    if capability is False and not cfg.force_vision:
        raise SystemExit(
            "This server does not accept image input, so it cannot read the\n"
            "screenshot. Sending it anyway would likely produce a confident\n"
            "answer about an image the model never saw.\n\n"
            f"  server says vision: false   ({cfg.root}/props)\n\n"
            "For Strata, restart it with image support enabled:\n"
            "  --vision yes\n"
            "or answer 'yes' to the Images? prompt in its setup. Starting\n"
            "without that option is what disabled it here.\n\n"
            "To send the request anyway: --force-vision"
        )
    if capability is None:
        cfg.say("  (could not determine vision support; attempting anyway)")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _data_uri(image)}},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    if cfg.effort != "off":
        payload["reasoning_effort"] = cfg.effort

    cfg.model = model  # so the report records what was actually used
    endpoint = f"{cfg.base.rstrip('/')}/chat/completions"
    cfg.say(f"POST {endpoint}  (model={model!r}, effort={cfg.effort})")
    status, body = http_json(
        endpoint, payload, timeout=max(cfg.timeout * 4, 300.0), token=cfg.api_key
    )

    if status != 200 or not isinstance(body, dict):
        hint = ""
        if status == 404 and isinstance(body, str) and "vision" in body.lower():
            hint = "\n  The server may have been started without image support (--vision yes)."
        raise SystemExit(f"Vision request failed (HTTP {status}).{hint}\n{_short(body, 700)}")

    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise SystemExit(f"Unexpected response shape:\n{_short(body, 700)}") from None

    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    answer = (content or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()

    usage = body.get("usage") or {}
    if usage:
        cfg.say(f"  tokens: {usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out")
    return answer, reasoning


# --------------------------------------------------------------------------
# stage 3: deterministic market data
# --------------------------------------------------------------------------


def _pct(last: float, prev: float | None) -> float | None:
    return None if not prev else (last - prev) / prev * 100.0


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def sector_data(cfg: Config) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch last-session change per sector ETF from daily closes."""
    rows: list[dict[str, Any]] = []
    error: str | None = None

    for symbol, sector in SECTOR_ETFS.items():
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            "?range=1mo&interval=1d"
        )
        status, body = http_json(url, timeout=25.0)
        if status != 200 or not isinstance(body, dict):
            error = f"{symbol}: HTTP {status}"
            continue
        try:
            result = body["chart"]["result"][0]
            stamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError):
            error = f"{symbol}: unexpected payload"
            continue

        series = [(t, c) for t, c in zip(stamps, closes) if isinstance(c, (int, float))]
        if len(series) < 2:
            error = f"{symbol}: too few closes"
            continue

        (_, last), (_, prev) = series[-1], series[-2]
        five = series[-6][1] if len(series) >= 6 else None
        month = series[0][1]

        rows.append(
            {
                "symbol": symbol,
                "sector": sector,
                "close": last,
                "date": datetime.fromtimestamp(series[-1][0], tz=timezone.utc)
                .date()
                .isoformat(),
                "day": _pct(last, prev),
                "five_day": _pct(last, five) if five else None,
                "month": _pct(last, month),
            }
        )
        cfg.say(f"  {symbol} ok ({rows[-1]['date']}, {_fmt_pct(rows[-1]['day'])})")

    return rows, error


# --------------------------------------------------------------------------
# stage 3b: fact-check the model's own numbers
# --------------------------------------------------------------------------

# Matches "MSFT (+2.38%)", "**NFLX** (-1.77%)", "BRK.B +0.89 %" and friends.
CLAIM_RE = re.compile(
    r"\b([A-Z]{1,5}(?:\.[A-Z]{1,2})?)\*{0,2}\s*\(?\s*([+-]?\d+(?:\.\d+)?)\s*%"
)
CLAIM_STOPWORDS = {
    "CONFIDENCE", "UNREADABLE", "STRONGEST", "WEAKEST", "SECTORS", "SECTOR",
    "NOTABLE", "TICKERS", "TICKER", "HIGH", "LOW", "MEDIUM", "USD", "ETF",
}


def last_session_change(cfg: Config, symbol: str) -> tuple[float, str] | None:
    """One symbol's most recent completed-session change, or None."""
    for candidate in (symbol, symbol.replace(".", "-")):
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{candidate}"
            "?range=5d&interval=1d"
        )
        status, body = http_json(url, timeout=20.0)
        if status != 200 or not isinstance(body, dict):
            continue
        try:
            result = body["chart"]["result"][0]
            stamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            series = [(t, c) for t, c in zip(stamps, closes) if isinstance(c, (int, float))]
            (t1, c1), (_, c0) = series[-1], series[-2]
            date = datetime.fromtimestamp(t1, tz=timezone.utc).date().isoformat()
            return (c1 - c0) / c0 * 100.0, date
        except (KeyError, IndexError, TypeError, ZeroDivisionError):
            continue
    return None


def factcheck_claims(cfg: Config, answer: str) -> list[dict[str, Any]]:
    """Verify every TICKER (+x.xx%) the model asserted, against real closes.

    Vision models read the digits off a heat map very accurately but confuse
    red with green often enough to matter, so the sign is the thing to check.
    """
    claimed: dict[str, float] = {}
    for symbol, pct_text in CLAIM_RE.findall(answer):
        if symbol in CLAIM_STOPWORDS or symbol in claimed:
            continue
        claimed[symbol] = float(pct_text)

    if not claimed:
        return []

    cfg.say(f"Fact-checking {len(claimed)} claimed figure(s)...")
    rows: list[dict[str, Any]] = []
    for symbol, want in claimed.items():
        found = last_session_change(cfg, symbol)
        if not found:
            rows.append({"symbol": symbol, "claimed": want, "actual": None})
            continue
        actual, date = found
        sign_flip = (want >= 0) != (actual >= 0)
        rows.append(
            {
                "symbol": symbol,
                "claimed": want,
                "actual": actual,
                "date": date,
                "sign_flip": sign_flip,
                "close_match": abs(actual - want) < 0.05,
            }
        )
    return rows


# --------------------------------------------------------------------------
# stage 3c: the one-screen market picture
# --------------------------------------------------------------------------

# Coarse character read: were defensives or cyclicals bid?
DEFENSIVE_SECTORS = {"XLU", "XLP", "XLV", "XLRE"}
CYCLICAL_SECTORS = {"XLK", "XLY", "XLF", "XLI", "XLB", "XLE"}


def _mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _tone(advancing: int, declining: int, total: int) -> str:
    if advancing >= total - 1:
        return "broad advance"
    if advancing >= 0.75 * total:
        return "advance"
    if advancing > declining:
        return "mixed, upward tilt"
    if advancing == declining:
        return "evenly split"
    if declining >= total - 1:
        return "broad decline"
    if declining >= 0.75 * total:
        return "decline"
    return "mixed, downward tilt"


def _character(rows: list[dict[str, Any]]) -> str:
    defensive = _mean([r["day"] for r in rows if r["symbol"] in DEFENSIVE_SECTORS])
    cyclical = _mean([r["day"] for r in rows if r["symbol"] in CYCLICAL_SECTORS])
    if defensive is None or cyclical is None:
        return "character unknown"
    gap = defensive - cyclical
    if gap > 0.5:
        return "defensive leadership"
    if gap < -0.5:
        return "cyclical leadership"
    return "balanced leadership"


def market_picture_facts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The numbers behind the summary, reusable as structured output."""
    dated = [r for r in rows if r.get("day") is not None]
    if not dated:
        raise ValueError("no sector rows carry a session change")

    ranked = sorted(dated, key=lambda r: -r["day"])
    advancing = sum(1 for r in dated if r["day"] > 0)
    declining = sum(1 for r in dated if r["day"] < 0)
    five = [r for r in dated if r.get("five_day") is not None]
    month = [r for r in dated if r.get("month") is not None]

    return {
        "session_dates": sorted({r["date"] for r in dated}),
        "sectors_counted": len(dated),
        "advancing": advancing,
        "declining": declining,
        "unchanged": len(dated) - advancing - declining,
        "leaders": [
            {"symbol": r["symbol"], "sector": r["sector"], "change_pct": round(r["day"], 4)}
            for r in ranked[:3]
        ],
        "laggards": [
            {"symbol": r["symbol"], "sector": r["sector"], "change_pct": round(r["day"], 4)}
            for r in reversed(ranked[-3:])
        ],
        "dispersion_pp": round(ranked[0]["day"] - ranked[-1]["day"], 4),
        "tone": _tone(advancing, declining, len(dated)),
        "character": _character(dated),
        "five_day_best": (
            {"sector": max(five, key=lambda r: r["five_day"])["sector"],
             "change_pct": round(max(five, key=lambda r: r["five_day"])["five_day"], 4)}
            if five else None
        ),
        "five_day_worst": (
            {"sector": min(five, key=lambda r: r["five_day"])["sector"],
             "change_pct": round(min(five, key=lambda r: r["five_day"])["five_day"], 4)}
            if five else None
        ),
        "month_best": (
            {"sector": max(month, key=lambda r: r["month"])["sector"],
             "change_pct": round(max(month, key=lambda r: r["month"])["month"], 4)}
            if month else None
        ),
        "month_worst": (
            {"sector": min(month, key=lambda r: r["month"])["sector"],
             "change_pct": round(min(month, key=lambda r: r["month"])["month"], 4)}
            if month else None
        ),
    }


def build_market_picture(
    rows: list[dict[str, Any]],
    *,
    generated: datetime | None = None,
    chart_read: str | None = None,
    chart_claims: list[dict[str, Any]] | None = None,
) -> str:
    """A one-screen US market summary, built from verified closes.

    Session age is measured in UTC calendar days rather than converted to US
    Eastern, so this needs no IANA timezone database (which Windows Python
    does not ship by default).
    """
    facts = market_picture_facts(rows)
    now = generated or datetime.now().astimezone()
    dates = facts["session_dates"]
    session = dates[-1] if dates else "unknown"

    age_text = "unknown"
    try:
        age = (datetime.now(timezone.utc).date() - date.fromisoformat(session)).days
        age_text = f"{age} calendar day(s) before today" if age > 0 else "today"
    except ValueError:
        pass

    def describe(items: list[dict[str, Any]]) -> str:
        return ", ".join(f"{i['sector']} {i['change_pct']:+.2f}%" for i in items)

    breadth = f"{facts['advancing']} of {facts['sectors_counted']} sectors higher"
    if facts["declining"]:
        breadth += f", {facts['declining']} lower"
    if facts["unchanged"]:
        breadth += f", {facts['unchanged']} unchanged"

    line = "=" * 68
    out = [
        "MARKET PICTURE - USA",
        line,
        f"generated   : {now.isoformat(timespec='seconds')}",
        f"session     : {session} (most recent completed US session)",
        f"session age : {age_text}",
        "",
        f"BREADTH     : {breadth}",
        f"LEADERS     : {describe(facts['leaders'])}",
        f"LAGGARDS    : {describe(facts['laggards'])}",
        f"DISPERSION  : {facts['dispersion_pp']:.2f} pp between the best and worst sector",
        f"TONE        : {facts['tone']}; {facts['character']}",
    ]
    if facts["five_day_best"] and facts["five_day_worst"]:
        out.append(
            f"5-DAY       : {facts['five_day_best']['sector']} "
            f"{facts['five_day_best']['change_pct']:+.2f}% best, "
            f"{facts['five_day_worst']['sector']} "
            f"{facts['five_day_worst']['change_pct']:+.2f}% worst"
        )
    if facts["month_best"] and facts["month_worst"]:
        out.append(
            f"1-MONTH     : {facts['month_best']['sector']} "
            f"{facts['month_best']['change_pct']:+.2f}% best, "
            f"{facts['month_worst']['sector']} "
            f"{facts['month_worst']['change_pct']:+.2f}% worst"
        )

    out += [
        "",
        "PROVENANCE  : SPDR sector ETFs, cap-weighted within each sector.",
        "              Deterministic arithmetic on published closes; no model involved.",
    ]

    if chart_read:
        claims = chart_claims or []
        checked = sum(1 for c in claims if c.get("actual") is not None)
        flips = sum(1 for c in claims if c.get("sign_flip"))
        out += [
            "",
            "CHART READ (UNVERIFIED - a vision model describing a screenshot)",
            "-" * 68,
            "\n".join("  " + ln for ln in chart_read.strip().splitlines()),
        ]
        if checked:
            out += ["", f"FACT-CHECK  : {flips} of {checked} claimed figures had the wrong sign."]

    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# stage 4: the report
# --------------------------------------------------------------------------


def build_report(
    cfg: Config,
    *,
    shot: Path | None,
    answer: str,
    reasoning: str,
    rows: list[dict[str, Any]],
    data_error: str | None,
    claims: list[dict[str, Any]],
) -> str:
    now = datetime.now().astimezone()
    line = "=" * 72
    out: list[str] = [
        "TRADINGVIEW HEAT MAP - VISION REPORT",
        line,
        f"generated      : {now.isoformat(timespec='seconds')}",
        f"local timezone : {now.tzname()}",
        f"source url     : {cfg.url}",
        f"model server   : {cfg.base}",
        f"model id       : {cfg.model}",
        "",
    ]

    # -- 1. capture --------------------------------------------------------
    out += ["1. CAPTURE", line]
    if shot and shot.exists():
        out += [
            f"screenshot : {shot}",
            f"size       : {shot.stat().st_size / 1024:.0f} KB",
            f"viewport   : {cfg.width}x{cfg.height} @ {cfg.scale}x scale",
        ]
    elif cfg.skip_capture and cfg.image is None:
        out.append("skipped (--skip-capture, no --image given)")
    else:
        out.append("not captured")
    out.append("")

    # -- 2. vision ---------------------------------------------------------
    out += [
        "2. VISION MODEL READ",
        line,
        "!! Model-generated. This is an interpretation of a picture, not data.",
        "!! Treat every ticker and number below as unverified.",
        "",
    ]
    if cfg.skip_vision:
        out.append("skipped (--skip-vision)")
    elif answer:
        out.append(answer)
    else:
        out.append("(the model returned no text)")
    if reasoning and cfg.include_reasoning:
        out += ["", "--- model reasoning (verbatim) ---", reasoning]
    out.append("")

    # -- 3. verified data --------------------------------------------------
    out += [
        "3. VERIFIED MARKET DATA",
        line,
        "Deterministic cross-check: SPDR sector ETF closes, no model involved.",
        "",
    ]
    if cfg.skip_market_data:
        out.append("skipped (--skip-market-data)")
    elif not rows:
        out.append(f"unavailable ({data_error or 'no rows returned'})")
    else:
        dates = sorted({r["date"] for r in rows})
        out.append(f"session(s) covered: {', '.join(dates)}")
        out.append("")
        out.append(f"{'#':>2}  {'sector':<24}{'last':>9}{'5-day':>9}{'1-month':>9}")
        out.append("-" * 72)
        ranked = sorted(rows, key=lambda r: (r["day"] is None, -(r["day"] or 0)))
        for i, r in enumerate(ranked, 1):
            out.append(
                f"{i:>2}  {r['sector']:<24}{_fmt_pct(r['day']):>9}"
                f"{_fmt_pct(r['five_day']):>9}{_fmt_pct(r['month']):>9}"
            )
        out.append("")
        out.append("ETF proxies, cap-weighted within each sector.")
        if data_error:
            out.append(f"partial: {data_error}")
    out.append("")

    # -- 4. fact-check -----------------------------------------------------
    out += [
        "4. FACT-CHECK OF THE MODEL'S OWN NUMBERS",
        line,
        "Every TICKER (+x.xx%) the model stated in section 2, re-checked against",
        "real closes. Magnitudes are usually read accurately; the SIGN is where",
        "vision models fail, because it means telling red from green.",
        "",
    ]
    if cfg.skip_factcheck:
        out.append("skipped (--no-factcheck)")
    elif not claims:
        out.append("no explicit ticker figures found in the model's answer")
    else:
        out.append(f"{'ticker':<9}{'claimed':>10}{'actual':>10}   verdict")
        out.append("-" * 72)
        flips = 0
        for c in claims:
            if c["actual"] is None:
                out.append(f"{c['symbol']:<9}{_fmt_pct(c['claimed']):>10}{'n/a':>10}   unverified")
                continue
            # Compare magnitudes, so a correct number with a wrong sign is
            # reported as a colour misread rather than a wrong ticker.
            mag_match = abs(abs(c["actual"]) - abs(c["claimed"])) < 0.05
            if c["sign_flip"] and mag_match:
                verdict = "SIGN FLIPPED (colour misread)"
                flips += 1
            elif c["sign_flip"]:
                verdict = "MISREAD (ticker or value)"
                flips += 1
            elif mag_match:
                verdict = "ok"
            else:
                verdict = "value differs"
            out.append(
                f"{c['symbol']:<9}{_fmt_pct(c['claimed']):>10}"
                f"{_fmt_pct(c['actual']):>10}   {verdict}"
            )
        out.append("")
        checked = sum(1 for c in claims if c["actual"] is not None)
        out.append(f"{flips} of {checked} checked claims had the wrong sign.")
        if flips:
            out.append("Treat every directional statement in section 2 as suspect.")
    out.append("")

    # -- 5. caveats --------------------------------------------------------
    out += [
        "5. CAVEATS",
        line,
        "- Section 2 is a language model reading a screenshot. Small tiles and",
        "  tiny text are exactly where such models hallucinate. It is a",
        "  qualitative read, never a source of numbers.",
        "- Section 3 is arithmetic on published closes, so it is exact -- but it",
        "  is ETF proxies, not TradingView's own tree map over individual stocks.",
        "- 'Last session' means the most recent completed session. Before the US",
        "  open, or on a weekend/holiday, that is the previous trading day.",
        "- Automated capture of a third-party site may conflict with its terms of",
        "  service. Check before running this on a schedule.",
        "",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(
        description="Screenshot a TradingView heat map, read it with a local vision "
        "model, and write the findings to a text file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--probe", action="store_true", help="report what the model server offers, then exit")
    p.add_argument("--url", default=DEFAULT_URL, help="heat-map page to capture")
    p.add_argument("--image", type=Path, help="use this screenshot instead of capturing")
    p.add_argument("--out", type=Path, help="report path (default reports/heatmap-<stamp>.txt)")

    p.add_argument("--base", default=DEFAULT_BASE, help="OpenAI-compatible base URL")
    p.add_argument("--model", default=None, help="model id (default: read from /v1/models)")
    p.add_argument("--api-key", default=None, help="bearer token, if the server wants one")
    p.add_argument(
        "--effort",
        default="none",
        choices=["none", "low", "medium", "high", "off"],
        help="reasoning effort; 'off' omits the field for non-Strata servers",
    )

    p.add_argument(
        "--engine",
        default="auto",
        choices=["auto", "playwright", "edge"],
        help="capture backend: 'edge' uses a system Chrome/Edge and needs no Python packages",
    )
    p.add_argument("--browser", default=None, help="explicit path to chrome.exe / msedge.exe")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--scale", type=float, default=2.0, help="device scale factor (crisper small text)")
    p.add_argument("--wait", type=float, default=8.0, help="seconds to wait for tiles to render")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--full-page", action="store_true")
    p.add_argument("--timeout", type=float, default=60.0, help="HTTP/page timeout in seconds")

    p.add_argument("--skip-capture", action="store_true")
    p.add_argument(
        "--force-vision",
        action="store_true",
        help="send the image even when the server reports no vision support",
    )
    p.add_argument("--skip-vision", action="store_true")
    p.add_argument("--skip-market-data", action="store_true")
    p.add_argument(
        "--no-factcheck",
        action="store_true",
        dest="skip_factcheck",
        help="do not verify the ticker percentages the model claimed",
    )
    p.add_argument("--include-reasoning", action="store_true", help="include raw model reasoning")
    p.add_argument("--max-tokens", type=int, default=1600)
    p.add_argument("-q", "--quiet", action="store_true")

    a = p.parse_args(argv)
    return Config(**vars(a))


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)

    if cfg.probe:
        return probe(cfg)

    cfg.model = resolve_model(cfg)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shot = cfg.image
    rows: list[dict[str, Any]] = []
    data_error: str | None = None
    answer = reasoning = ""

    if cfg.skip_capture and shot is None:
        cfg.say("No capture requested (--skip-capture).")
    elif shot is None:
        shot = Path("reports") / f"heatmap-{stamp}.png"
        shot = capture(cfg, shot)
    else:
        if not shot.exists():
            raise SystemExit(f"--image does not exist: {shot}")
        cfg.say(f"Using existing image: {shot}")

    if cfg.skip_vision:
        cfg.say("Skipping vision (--skip-vision).")
    elif shot is None:
        cfg.say("No image available, so vision is skipped.")
    else:
        answer, reasoning = ask_vision(cfg, shot)
        cfg.say("Vision model answered." if answer else "Vision model returned nothing.")

    if cfg.skip_market_data:
        cfg.say("Skipping market data (--skip-market-data).")
    else:
        cfg.say("Fetching sector data...")
        rows, data_error = sector_data(cfg)

    claims: list[dict[str, Any]] = []
    if not cfg.skip_factcheck and answer:
        claims = factcheck_claims(cfg, answer)

    report = build_report(
        cfg,
        shot=shot,
        answer=answer,
        reasoning=reasoning,
        rows=rows,
        data_error=data_error,
        claims=claims,
    )

    out = cfg.out or Path("reports") / f"heatmap-report-{stamp}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    cfg.say(f"\nReport written: {out.resolve()}")
    print(f"\n{report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
