# chaiai

**English** | [简体中文](README.zh-CN.md)

**An AI agent that runs on your own computer.** Conversations, memory, files and tool execution all stay local — you bring your own model endpoint.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Node](https://img.shields.io/badge/node-22.20%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

> **Status: v0.1.1, early.** Contracts and APIs still move; not recommended as a production dependency.
> It is open-sourced to gather feedback first — structure, contracts and security boundaries: issues are welcome.

![Chat with tool execution](docs/assets/screenshots/conversation.png)

*A real session in a temporary workspace: tool calls, memory writes and the change log, as they happened.*

## The problem it solves

Most agent projects are "cloud service + web shell": your files get uploaded, the keys belong to someone else, and execution happens on a machine you cannot see. chaiai is the opposite — **it is a program you install on your PC**:

- File reads/writes, command execution and browser control all happen locally, and every step is visible in the UI
- Conversations, memory, settings and keys live in local SQLite; uninstalling does not delete them unless you tick the box
- The model runs only against the endpoint you configure — no relay, no telemetry

## Quick start

### Option 1: installer (recommended, Windows 10/11)

1. Download `chaiai-setup-<version>.exe` from [Releases](https://github.com/LaoTouAiLe/chaiai/releases)
2. Double-click to install (you can change the install location; the default is user-level, no admin rights needed)
3. It launches as a **desktop window** (not a browser tab). Add your endpoint and key in **Settings → Model connections**

> The installer is **not code-signed**, so Windows SmartScreen shows "Unknown publisher".
> Click **More info → Run anyway**. SHA-256 checksums are published in every release as `SHA256SUMS.txt`.

Install / uninstall / data location / fallbacks: see [packaging/使用说明.txt](packaging/使用说明.txt) (Chinese).

### Option 2: from source

Requirements: **Python 3.10+**, **Node 22.20+**, [uv](https://docs.astral.sh/uv/).

```powershell
cd local-agent-web
uv sync                 # add `--extra memory` for vector memory (Chroma + local ONNX embedding)
cd frontend
npm ci
npm run build
cd ..
uv run python -m local_agent.api
```

Open http://127.0.0.1:8000 . For development, run `npm run dev` in `frontend/` (5173, proxied to 8000).
Prefer a desktop window over a browser tab: `uv run python -m local_agent.api --window`.

Configuration: you can start with no `config.yaml` at all and add a model connection in the UI. If you do want a file,
copy `config.yaml.example` / `.env.example` — **do not overwrite an existing `config.yaml`**.

## Features

| Capability | In one line |
| --- | --- |
| Conversation and tool loop | The model decides which tools to call and how often; every step, argument, output and duration is visible |
| Approvals and permissions | Writes and commands need a one-time approval; destructive operations need a two-stage confirmation |
| Files and commands | Targeted reads/writes, full-text search, command execution, background jobs, persistent terminals |
| Browser | A separate Edge/Chrome instance, 13 actions, screenshots can be fed back to the model |
| Three-tier memory | Short-term compression, long-term facts, mid-term vector recall — all inspectable |
| Web search | Tavily / DeepSeek search API / Bing–DuckDuckGo HTML, with local or remote page fetching |
| Right-hand panel | File tree, search, git diff, change log, terminal, task center, session map |
| Extensions | MCP servers, `SKILL.md` skill packs, cron jobs |

### Conversation and tool loop

- One turn is a loop the model drives itself; **process, reasoning, arguments, output and duration** are shown separately and each can be expanded
- Read-only tools run in parallel; writes are serialized to avoid clobbering each other
- Each turn ends with a summary: total time, cache hit rate, token usage
- You can stop at any moment. Stopping ends that turn only — **it does not undo side effects already applied**, and the UI says which ones ran
- Failures are reported as failures: auth errors, rate limits and exhausted retries each get their own message

### Trace and review

- **Trace page**: lays one turn out on a timeline — every tool call with arguments, output, duration and failure reason. Read-only; it never feeds back into the conversation
- **Think**: shows the reasoning content the provider actually returned, never synthesized or filled in
- **Session map**: renders branches and follow-up questions as a card tree; you can reopen any branch and continue from there

![Trace page: per-turn metrics and tool calls](docs/assets/screenshots/trace.png)

![Session map: branch cards](docs/assets/screenshots/map.png)

### Approvals and permissions

| Action | Handling |
| --- | --- |
| Read files inside the workspace, list directories, search | no approval |
| Write files, run commands, start a terminal | approval card — "Allow once" applies to that one call only |
| Write outside the workspace | grant that directory first; grants are revocable, and reads still work after revoking |
| High-risk deletions | two-stage approval: the first "allow" asks again |

### Files and commands

| Capability | Notes |
| --- | --- |
| Read | Line-range reads; large files read in continuing windows with a content-version check so edited content is never spliced into one document |
| Write | Exact-string replacement (single match by default, multiple matches must be declared) or whole-file writes; if surrounding text changed, it refuses rather than guessing |
| Find | Directory listing, filename search, full-text search, grep / glob; paginated with cursors |
| Run | Command execution, detach to background jobs, interactive persistent terminals (continuous input/output, not one-shot) |
| Limits | Command timeouts, output caps and concurrency are configurable; Windows write-side sandbox derives from the permission tier |

### Browser

A separate Edge/Chrome instance that **does not borrow your personal browser profile**, 13 actions:
open, switch tab, navigate, snapshot, click, fill, press key, scroll, wait, screenshot, resize, download, close.
Screenshots are written into the workspace so the model can look at them.

### Three-tier memory and context budget

| Tier | Stores | How it is used |
| --- | --- | --- |
| Short-term | the current conversation | auto-compressed near the context limit; keeps the last 8 turns (configurable) plus key tool results |
| Long-term | facts, preferences, conclusions | injected across sessions; weighted, with confidence and aging, each entry listed and deletable |
| Mid-term | vectors of conversation summaries | semantic recall (optional deps: Chroma + embedding); threshold and top-k configurable |

Compression is not truncation: tool results have their own character budget (8k by default), and the kept-turn count
tightens automatically when the window is small.

### Web search

- Backends: **Tavily**, **DeepSeek search API**, Bing / DuckDuckGo HTML (no key needed)
- Page fetching: **local** (default) or delegated to DeepSeek (sends the URL to them, billed per call)
- Time-range and site filters are supported; HTML backends do not honor site filters and say so instead of pretending
- `408/429/5xx` are retried per the provider's `Retry-After` (with a cap); other 4xx fail once
- An empty result is "nothing found", not an error — the model is never tricked into switching backends

### DeepSeek specifics

The project is **not tied to** DeepSeek — any OpenAI-compatible endpoint works. But every DeepSeek pitfall hit during
development is handled in code:

| Problem | Handling |
| --- | --- |
| Thinking mode requires `reasoning_content` to be sent back verbatim, otherwise the **second request fails with HTTP 400** and the tool loop cannot finish | A minimal `ChatOpenAI` subclass handles the round trip and is **saved with the checkpoint**, so a restored history still rebuilds the original request |
| DeepSeek returns `reasoning_content: ""` (especially for direct tool calls) | The test is "field **exists**", not "content is non-empty"; an empty string is sent back as-is |
| The SDK sends `max_completion_tokens`, the provider only accepts `max_tokens` | Converted on the request side, with per-call overrides preserved |
| Summarization calls spend the whole budget on reasoning and produce no summary | Thinking is disabled for summarization on the official endpoint; the main decision path is unaffected |
| Cache-hit fields differ per provider | Read `prompt_tokens_details.cached_tokens` first, then fall back to the provider's raw field, and normalize into one ratio |

### Right-hand panel

A panel beside the conversation, so you can inspect while you chat:

| Tab | Content |
| --- | --- |
| Files | Workspace file tree, click to read, filter by path |
| Changes | Files changed in this session, `+N −M` per file, red/green diff |
| Browser | Live view and address bar of the controlled browser |
| Terminal | Persistent terminal I/O; commands need approval |
| Tasks | Cross-session task state and checkpoints |

### Tasks and automation

- **Task center**: background tasks across sessions with progress, checkpoints and failure reasons; final states that need a human are written by the human
- **Recovery**: after a process restart, unfinished tasks can be picked up — never silently re-run
- **Cron**: run a fixed prompt on a cron schedule (e.g. tidy a directory daily, watch a topic), run once immediately, view run history, enable/disable. Runs still go through the same approvals and permission boundaries

![Cron: schedule, next run and history](docs/assets/screenshots/cron.png)

### Extensions: MCP and skills

- **MCP**: stdio-based local MCP servers, with add/remove/edit, tool allow/deny lists and connection lifecycle
- **Skills**: `SKILL.md` skill packs that can be enabled/disabled; beyond bundled skills you can add your own skill directories (referenced, not copied)

### Attachments and multimodality

Images can be dragged into the input (4 MB per image; larger is rejected honestly, not truncated).
Whether a model can see images is **declared per connection** — if it is not declared, it is treated as unsupported.

### Settings

Model connections (multiple, testable, per-connection image support and context window), permission tiers, appearance,
task and budget settings, web search, long/mid-term memory and embeddings, MCP servers, skill toggles.
Keys are write-only: the API only ever reports "configured / not configured".

### Observability and diagnostics

- **Data center**: call counts, input/output tokens, **cache hit rate**, memory counts, daily trend, model distribution.
  The hit rate is computed from provider-side cache fields — the screenshot below is 25 consecutive calls in one session
  measured at **96.5%**; single turns show 98–99% on the trace page. Cache is provider-side prefix caching; short sessions
  or changing tool sets lower it, so **no stability is promised** — only your own machine's real numbers
- **Logs**: `启动错误.log` and `模型调用失败.log` (summaries only — no keys, no message bodies)
- **Self-check**: `chaiai.exe --diagnose` prints JSON covering config, frontend assets, the three databases and dependencies. It never reads keys

![Data center: usage, cache hits and memory](docs/assets/screenshots/data-center.png)

![Right-hand panel: changes and diff](docs/assets/screenshots/changes.png)

## Security boundaries (read first)

- **Binds to loopback only**; there is no account system and no login page. Exposing the port to a LAN means handing a terminal to everyone on that segment
- **Bring your own model**: the project provides no API keys and does not resell them
- **Approval is not optional**: writes and commands go through human approval. On Windows there is a write-side sandbox, but it constrains **writes only** — reads and network are not isolated
- **Commands can reach the network on their own**; there are no CPU/memory limits and no network isolation
- **The installer is not code-signed**: SmartScreen shows "Unknown publisher" — click **More info → Run anyway**
- **Windows only**: the write-side sandbox, desktop window and system queries depend on Windows APIs

## Where data lives

| Content | Location |
| --- | --- |
| Program | `%LOCALAPPDATA%\Programs\chaiai` |
| Settings DB, conversations, memory, attachments | `%LOCALAPPDATA%\chaiai` (**source runs use the same place**, not the code directory) |
| Runtime logs | `启动错误.log`, `模型调用失败.log` in the same directory |

The data root can be moved to any absolute path via `data_root` (config.yaml) or `CHAIAI_HOME`.
Old data is not migrated automatically; the startup banner prints both locations.
**Uninstalling does not delete data** — unless you explicitly tick "also delete the data directory".
Upgrading means running the new installer over the old one; the data directory is untouched.

## Tech stack

| Layer | Used |
| --- | --- |
| Core | Python 3.10+, LangGraph (tool loop + checkpoints), Pydantic |
| Service | FastAPI + Uvicorn (single process, single worker), SSE for streaming events |
| Storage | SQLite (conversations, settings, memory metadata), Chroma (optional, mid-term memory) |
| UI | React 19 + Vite 8 + TypeScript, TanStack Query, Zustand |
| Packaging | PyInstaller (onedir) + Inno Setup; desktop window via WebView2 (pywebview) |

## Project layout

```
chaiai/
├─ local-agent-web/     main program: Python backend (src/) + frontend/ + tests/
├─ docs/                design and records: plans / audits / lessons / benchmarks / archive
├─ packaging/           release material: Inno Setup script, usage notes, bundled binaries
├─ skills/              bundled skills
├─ website/             static documentation site
└─ ARCHITECTURE.md      contracts and boundaries (read before changing behaviour)
```

## FAQ

**Multi-user or server deployment?**
No. The design is single-user, single-process, local, with no account system. Multi-user needs authentication and
per-session isolation first.

**Why Windows only?**
The write-side sandbox (restricted token + ACLs), the desktop window and system queries depend on native Windows APIs.

**Which models?**
Any OpenAI-compatible endpoint (`base_url` + `model`). Fill the context window honestly from the model's docs — a wrong
value makes the compression budget wrong. DeepSeek thinking-mode round-trips are handled.

**Why is the cache hit rate not always 90%+?**
It is provider-side prefix caching: high when the same system prompt and tool definitions are reused. Long sessions with
follow-ups commonly show 70–90%; short sessions or changing tool sets drop. The data center reports real local numbers only.

**Is my data uploaded?**
Not by the agent itself. But model calls, web search and DeepSeek fetching send the corresponding content to **the endpoint
you configured** — that is the feature, not a side effect.

**How to report bugs?**
See [CONTRIBUTING.md](CONTRIBUTING.md). Please attach the output of `chaiai.exe --diagnose` (it does not read keys).

## Documentation

| What you want | Where |
| --- | --- |
| Startup, configuration, UI walkthrough | [docs/DEVELOPING.md](docs/DEVELOPING.md) |
| Contracts, responsibility boundaries, security model | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Contribution rules | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Current phase and next steps | [v1.0 plan](docs/plans/V1_0_DEVELOPMENT_PLAN.md) |
| Future directions (no promises on schedule) | [roadmap notes](docs/planning/FUTURE_WORK.md) |
| Source-code walkthrough | [lessons](docs/lessons/README.md) (line numbers may have drifted) |

> Documentation is **mostly in Chinese**, and so are code comments. That does not block reading the code, but it does raise
> the bar for outside contributions — PRs that improve this are welcome.

## License

[MIT](LICENSE)
