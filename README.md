# TV_Service

Turns a TradingView heat map into a factual market summary: point a local vision
model at a screenshot, then **check everything it says against real market data**.

Two entry points share one core — a command line, and a Model Context Protocol
service an AI agent can call as tools.

```
https://github.com/AndrewMcAlister/TV_Service
```

## The parts

| Part | Role |
|---|---|
| `heatmap_report.py` | **The pipeline and the CLI.** Capture → vision → verified data → fact-check → report. All the logic lives here; everything else wraps it. |
| `heatmap_mcp.py` | **The MCP server.** Exposes the pipeline as six tools plus report resources. Speaks stdio and Streamable HTTP. Standard library only — no `mcp` package. |
| `service/` | Windows service scripts (install / status / uninstall) built on `nssm`. |
| `tests/` | Two protocol suites, a stdio driver, and a mock model server — so the whole thing can be exercised without a model or a browser. |
| `reports/` | Generated reports and screenshots. |
| `logs/` | `latest_market_picture_USA.txt`, overwritten on every call — a "latest state" file, never appended. |

## The external pieces

| Piece | What it is | How TV_Service uses it |
|---|---|---|
| **Strata** — `D:\Strata`, port 8080 | Local inference engine (125B MoE), OpenAI-compatible, vision enabled | Reads the screenshot. Optional: without it, `--skip-vision` still produces the verified half. |
| **TradingView** | The heat-map web page | Screenshotted by a headless browser (Edge, or Playwright if installed). |
| **Yahoo Finance** | Public chart endpoint | Source of the verified closes, and of every fact-check. |
| **DSH harness** — `D:\deepseek-harness`, port 3080 | The agent host | Loads `heatmap_mcp.py` as an MCP plugin, configured in the profile's `cordis.patch.yml`. |
| **nssm** | Service wrapper | Runs the HTTP mode of the MCP server as the `tv_service` Windows service. |

## How they interact

```
                 TradingView heat map  (web page)
                              |
              headless browser screenshot (edge | playwright)
                              v
                        screenshot .png
                              |
          +-------------------+-------------------+
          |                                       |
          v                                       v
   VISION  (unverified)                   VERIFIED DATA  (exact)
   heatmap_mcp  ->  Strata :8080          SPDR sector ETFs  <-  Yahoo
   "what the picture shows"               arithmetic on real closes
          |                                       |
          +-------------------+-------------------+
                              v
                          FACT-CHECK
          every TICKER (+x.xx%) the model claimed,
          re-read against real Yahoo closes
                              |
                              v
                 report .txt   +   logs/latest_market_picture_USA.txt
```

**The design rule this shape enforces:** the model never supplies a number, only
a lead. Vision reads magnitudes well but confuses red with green on small tiles —
measured at roughly 15% of claimed figures having the wrong sign, including one
case where it read `ABBV` as `ABVX`. So the two readings are produced separately
and the model's is audited against the other.

## Two ways in, one core

```powershell
# CLI - capture, read, verify, write a report
python heatmap_report.py --engine edge

# MCP server - stdio, for a client that spawns it on demand
python heatmap_mcp.py

# MCP server - HTTP, for the always-on service
python heatmap_mcp.py --http --port 8099
```

The six MCP tools are `market_picture`, `heatmap_report`, `sector_performance`,
`analyze_heatmap_image`, `capture_heatmap` and `probe_model_server`.
`market_picture` is the cheap one (~8 s, no model); `heatmap_report` is the full
pass (~30 s).

## The Windows service

`tv_service` runs the HTTP server so any MCP client can reach it at
`http://127.0.0.1:8099/mcp` without the harness being involved.

| | |
|---|---|
| Installed by | `service/install-tv-service.bat` (self-elevating, idempotent) |
| Runs | `python D:\TV_Service\heatmap_mcp.py --http --port 8099` |
| Start type | Automatic **(Delayed Start)** — waits for the boot to settle |
| Logs | `logs\service-stderr.log`, rotating at 1 MB |
| Bound to | `127.0.0.1` only, and **unauthenticated** — keep it local |

The harness itself (`dsh web`) is *not* a Windows service; it is a foreground
process launched by `StartDSHarness.bat` on your Desktop. DSH talks to the MCP
server over **stdio**, spawning it on demand, so it does not depend on the
service running.

## Where to go next

- **[docs/USAGE.md](docs/USAGE.md)** — setup, options, client registration,
  troubleshooting, and the full verification record.
- **`python heatmap_mcp.py --list-tools`** — the tool catalogue.
- **`tests/mcp_stdio_test.py`** — what "working" is defined to mean.
