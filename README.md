# TV_Service

TradingView heat map → local vision model → fact-checked text report, as both a
CLI and an MCP service.

```
https://github.com/AndrewMcAlister/TV_Service
```

A small Python app that does the three things you asked for:

1. **Captures** a TradingView heat-map page as a screenshot (headless Chromium via Playwright).
2. **Reads** that screenshot with a local vision model over an OpenAI-compatible API.
3. **Writes** the findings, plus an independent numeric cross-check, to a `.txt` file.

## Why there is a section 3

A vision model reading a dense heat map is genuinely unreliable: the tiles are
small and the text is tiny, which is exactly where these models invent plausible
tickers and percentages. So the report keeps the model's read and the real
numbers in **separate, clearly labelled sections**. The model section is a
qualitative impression; the ETF section is arithmetic on published closes.

If the two disagree, trust section 3.

## Local model servers

The app speaks the OpenAI Chat Completions API, so it works with any server that
implements it. Verified against [Strata](https://github.com/Niko1221/Strata),
which serves on `http://127.0.0.1:8080/v1`:

| Server | `--base` |
|---|---|
| Strata | `http://127.0.0.1:8080/v1` (default) |
| Ollama | `http://127.0.0.1:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM | `http://127.0.0.1:8000/v1` |

**Image input must be enabled on the server.** For Strata that is `--vision yes`
at setup time, or answering *yes* to the **Images?** prompt. Without it the model
cannot see the screenshot at all.

## Install

Nothing is strictly required. There are two capture backends:

| `--engine` | Needs | Notes |
|---|---|---|
| `edge` | A system Chrome or Edge | No Python packages at all. Uses the browser's own `--screenshot` flag. |
| `playwright` | `pip install -r requirements.txt` + `playwright install chromium` | More control (consent-banner clicks, network-idle waits). |

`--engine auto` (the default) picks Playwright when it is importable and falls
back to Edge otherwise. To force the zero-dependency path:

```powershell
python heatmap_report.py --engine edge
```

If you always pass `--image`, no backend is needed at all.

## Use

```powershell
# Check what your server is offering before anything else
python heatmap_report.py --probe

# Full run: capture, read, cross-check, write the report
python heatmap_report.py --vision yes

# Analyse a screenshot you already took
python heatmap_report.py --image C:\path\to\shot.png

# No browser and no model: just the verified sector numbers
python heatmap_report.py --skip-capture --skip-vision
```

Findings land in `reports/`, next to the screenshot they came from:

```
reports/
  heatmap-20261012-214700.png          the exact image the model saw
  heatmap-report-20261012-214700.txt   the report
```

## Options worth knowing

| Flag | Why |
|---|---|
| `--probe` | Lists `/health`, `/v1/models` and whether vision is on. Start here. |
| `--engine edge` | Zero-dependency capture via a system Chrome/Edge. |
| `--browser` | Explicit path to `chrome.exe` / `msedge.exe`. |
| `--scale 2` | Renders at 2x. Higher resolution materially improves how much the model can read. |
| `--wait 8` | Seconds to let the tiles render. Raise it on a slow connection. |
| `--force-vision` | Send the image even when the server reports no vision support. |
| `--no-factcheck` | Skip re-checking the model's claimed ticker percentages. |
| `--effort off` | Drops `reasoning_effort`, which is a Strata extension other servers may reject. |
| `--image` | Decouples the app from the browser entirely. |
| `--out` | Choose the report path. |

## If something fails

**`--probe` reports nothing answering.** The server is not running, or `--base`
is wrong. Check the port.

**Vision request fails, or the model says it sees no image.** The server was
started without image support. Restart it with vision enabled.

**The report says the model found the image "unreadable".** That is the honest
answer for a small or blurry capture. Raise `--width`, `--height` and `--scale`,
or crop the heat map to the region you care about and pass it with `--image`.

**Playwright errors on launch.** Run `playwright install chromium`. On a machine
where the sandbox blocks browser launches, use `--image` instead.

**A headless browser produces no screenshot.** Some restricted environments deny
a browser the pipes and process handles it needs. Symptom: Playwright fails with
`spawn EPERM`, or Edge exits non-zero with `OpenProcess: Access is denied`. This
is an environment restriction, not a bug in the app; it does not occur on a
normal desktop. Work around it with `--image` and a screenshot you took yourself.

## Verification status

What was actually tested, on 2026-10-12:

| Stage | Status |
|---|---|
| Capture via `--engine edge` | **Verified.** Real screenshot of the TradingView Stock Heatmap, fully legible, 829 KB (`reports/edge-test.png`). |
| Vision request shape | **Verified against a mock.** Payload is standard OpenAI: `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}` alongside a text part, `temperature: 0`. |
| Refusal on a text-only server | **Verified.** Exits 1 with an explanation rather than sending an image the model cannot see. |
| Report writing + market-data cross-check | **Verified.** Correct ranked sector table written to `reports/`. |
| Vision against a real vision model | **Verified.** Strata + `qwen3.8-flash-next-iq3_s`, real heat map, 26 s end to end. |
| Fact-check of the model's numbers | **Verified, and it caught real errors.** See below. |

### What the fact-check found

Run against a real 125B vision model reading a real heat map, and checked
against actual closes:

- **Magnitudes were read essentially perfectly.** 15 of 18 claimed percentages
  matched the real close to the cent.
- **Signs were wrong on 3 of 18.** `NFLX` claimed `+1.77%` when it fell `-1.77%`;
  `XOM` and `CVX` claimed negative when both rose. Near-zero moves are visually
  ambiguous, and even a careful human reading the same image got one wrong.
- **One was a ticker misread, not a sign error.** The model reported `ABVX`
  `+1.49%`; the map actually showed `ABBV` `+1.49%`. Different company, and it
  flipped that ticker's direction.

This is why section 4 exists, and why section 2's directional claims should never
be used without it. A heat map is a *picture* of data; the model is describing the
picture, not reading the data.

`tests/mock_openai_server.py` exercises the whole pipeline with no model at all:

```powershell
python tests\mock_openai_server.py --port 8099 --vision yes
python heatmap_report.py --base http://127.0.0.1:8099/v1 --image reports\edge-test.png --skip-capture
```

It logs every request to `tests/requests.jsonl`, which is how you inspect the
exact payload the app produces.

## Running as an MCP service

`heatmap_mcp.py` exposes the pipeline over the Model Context Protocol, so any
MCP client can call it as a tool. Standard library only — no `mcp` package, no
install step.

```powershell
python heatmap_mcp.py                     # stdio (what most clients want)
python heatmap_mcp.py --http --port 8099  # Streamable HTTP at /mcp
python heatmap_mcp.py --list-tools        # print the catalogue
```

### Tools

| Tool | What it does | Typical |
|---|---|---|
| `market_picture` | One-screen US market summary. Also overwrites `logs/latest_market_picture_USA.txt`. | ~8 s (~25 s with the chart read) |
| `heatmap_report` | Full pipeline: capture, vision, cross-check, fact-check, write the report | 20-40 s |
| `sector_performance` | Ranked sector ETFs. No browser, no model. | ~8 s |
| `analyze_heatmap_image` | Vision + fact-check on a screenshot you already have | ~15 s |
| `capture_heatmap` | Screenshot only | ~10 s |
| `probe_model_server` | Is the server up, which model, is vision enabled | <1 s |

### The market picture

`market_picture` returns a compact summary and **writes the identical text** to
`logs/latest_market_picture_USA.txt`, overwriting the previous copy every time —
so that file always holds the latest picture and never grows.

```
MARKET PICTURE - USA
====================================================================
generated   : 2026-10-12T22:40:36+11:00
session     : 2026-10-09 (most recent completed US session)
session age : 3 calendar day(s) before today

BREADTH     : 9 of 11 sectors higher, 2 lower
LEADERS     : Real Estate +1.86%, Health Care +1.58%, Consumer Discretionary +1.02%
LAGGARDS    : Communication Services -1.51%, Energy -0.25%, Consumer Staples +0.01%
DISPERSION  : 3.37 pp between the best and worst sector
TONE        : advance; defensive leadership
5-DAY       : Utilities +3.97% best, Technology -0.52% worst
1-MONTH     : Technology +7.32% best, Financials -3.76% worst

PROVENANCE  : SPDR sector ETFs, cap-weighted within each sector.
              Deterministic arithmetic on published closes; no model involved.
```

`TONE` is the breadth reading (broad advance → broad decline). `CHARACTER` is
whether defensives (XLU, XLP, XLV, XLRE) or cyclicals (XLK, XLY, XLF, XLI, XLB,
XLE) led — a five-point gap is the threshold, so "balanced" is a real answer.

Pass `include_chart_read: true` to append a vision model's reading of the chart,
fact-checked against real closes. It is a deliberately terse prompt — at most six
lines, one sector each — because this lands in a one-screen file:

```
CHART READ (UNVERIFIED - a vision model describing a screenshot)
--------------------------------------------------------------------
  Retail trade, AMZN +3.29%
  Technology services, MSFT +2.38%, GOOGL +0.97%
  Electronic technology, NVDA -0.52%, AAPL -1.11%

FACT-CHECK  : 1 of 10 claimed figures had the wrong sign.
```

The default is the deterministic path, because it is four times faster and has
nothing in it that can be wrong.

Set `TV_HEATMAP_LOGS` to put the log file somewhere other than `logs/` beside the
script — useful when the project directory is not writable by whatever runs the
server.

`heatmap_report`, `sector_performance` and `analyze_heatmap_image` also return
`structuredContent` — including `claims_checked` and `claims_with_wrong_sign` —
so a client can use the numbers without parsing prose.

Report files are published as MCP resources at `heatmap://reports/<filename>`.

### Registering it

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tv-service": {
      "command": "python",
      "args": ["D:\\TV_Service\\heatmap_mcp.py"]
    }
  }
}
```

**DeepSeek Harness**, stdio transport:

```yaml
- insert:
    - id: mcp-tv-service
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: tv_service
        transport: stdio
        command: python
        args: ['D:\TV_Service\heatmap_mcp.py']
        toolCallTimeoutMs: 300000
```

**DeepSeek Harness**, against a long-running HTTP service:

```yaml
- insert:
    - id: mcp-tv-service
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: tv_service
        transport: streamable-http
        url: http://127.0.0.1:8099/mcp
        toolCallTimeoutMs: 300000
```

### A new plugin needs the `insert:` form

`cordis.patch.yml` is a list of *patch* entries. A bare entry targets an
**existing** entry id and only overrides its config, so adding a plugin that way
fails at boot with:

```
patch: entry "mcp-tv-service" not found
```

Wrap new plugins in an `insert:` list, as above. The failure is silent in the
running harness because `failOnStartupError` defaults to `false` — the tools
simply never appear. `dsh --profile web --dump-config` prints any such error.
### The harness must be restarted

Adding the entry above edits the profile's `cordis.patch.yml`, which is read when
the harness composes its plugin tree at start-up. The `tv_service_*` tools
therefore appear only once the harness is restarted; editing the file under a
running harness has no effect until then. `failOnStartupError` defaults to
`false`, so if the plugin cannot load the harness still starts and simply omits
the tools rather than failing.

Confirm it loaded by asking the agent to call `tv_service_probe_model_server`, or
by running the server by hand:

```powershell
python D:\TV_Service\heatmap_mcp.py --list-tools
```
### Raise the tool timeout

DSH's `toolCallTimeoutMs` defaults to **60 seconds**. The full report takes
20-40 s — it drives a browser *and* a large local model — so the default is
uncomfortably close and will fail intermittently. Set it to `300000`.

### Why stdout purity is load-bearing

On the stdio transport stdout *is* the protocol channel. The pipeline prints
progress, so every tool call runs with `sys.stdout` redirected to stderr while
the protocol writer holds a private reference to the real stdout. One stray line
on stdout and the client rejects the whole stream as malformed. The test suite
asserts this explicitly.

### Testing it

```powershell
python tests\mcp_stdio_test.py    # protocol over stdio
python tests\mcp_http_test.py     # protocol over HTTP
```

Both drive the server through **file redirection rather than pipes**, because
restricted environments forbid the pipes `subprocess` would otherwise open. They
assert the handshake, tool and resource listing, a real tool call, stdout purity,
and the error paths (`-32601` unknown method, `-32602` unknown tool, `400` for a
malformed body).

## Two honest caveats

- **Terms of service.** TradingView's site is not yours to scrape on a schedule.
  Automated capture may violate their terms. Check before automating it. The
  `--image` path sidesteps this entirely, since you take the screenshot yourself.
- **This is not trading advice.** Sector ETF moves describe what already
  happened. Nothing here predicts anything.
