# MCP Memory Server

Persistent agent memory for [Blockbrain](https://blockbrain.ai) (or any MCP client) via Markdown files + SQLite FTS5 full-text search.

## Why?

Blockbrain agents don't persist memory between sessions. Every conversation starts from zero. This MCP server fixes that — the agent can save what it learned and recall it next time.

**How it works:**
- Agent solves a problem → calls `memory_save` with the solution
- Agent starts a new task → calls `memory_recall` to check for similar past problems
- All memories are stored as readable Markdown files + indexed in SQLite for fast search

## Tools

| Tool | Description |
|------|-------------|
| `memory_save` | Store a memory entry (topic, title, content) |
| `memory_recall` | Search memories by keyword/topic (FTS5) |
| `memory_list_topics` | List all stored topics with entry counts |
| `memory_delete` | Remove a memory entry by ID |

## Quick Start (Docker)

```bash
# 1. Clone
git clone https://github.com/lanlibbi/mcp-memory-server.git
cd mcp-memory-server

# 2. Configure
cp .env.example .env
# Edit .env — set MEMORY_API_KEY!
# Generate a key: openssl rand -hex 32

# 3. Run
docker compose up -d

# 4. Verify
curl http://localhost:8080/health
```

## Blockbrain Integration

1. **Expose the server publicly** — Blockbrain needs an HTTPS URL:
   - Option A: Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:8080`)
   - Option B: Any reverse proxy (nginx, Caddy, Traefik) with TLS
   - Option C: ngrok for testing (`ngrok http 8080`)

2. **Register in Blockbrain:**
   - Go to **Admin → Agents → MCP Servers**
   - Click **"+ Add MCP Server"**
   - Fill in:
     - **Name:** Memory Server
     - **Server URL:** `https://<your-public-url>/sse`
     - **Transport:** SSE
     - **Authentication:** API Key
     - **API Key:** *(the value from your .env)*
   - Save & activate

3. **Assign to an agent** and add instructions to the agent's system prompt:
   ```
   Before starting a new task, call memory_recall with keywords related to the task.
   After completing a task or learning something new, call memory_save with:
   - topic: a category for the task (e.g. "contract-review", "supplier-issue")
   - title: a short descriptive title
   - content: what was the problem, what was the solution, what was learned
   ```

## Configuration

All config via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_API_KEY` | *(empty = no auth)* | API key for X-API-Key header |
| `MEMORY_DATA_DIR` | `/data/memories` | Where Markdown files are stored |
| `MEMORY_PORT` | `8080` | HTTP port |
| `MEMORY_MAX_RESULTS` | `10` | Max search results per query |
| `LOG_LEVEL` | `info` | debug, info, warning, error |

## Storage

Memories are stored as **Markdown files** with YAML frontmatter:

```
/data/memories/
├── contract-review/
│   ├── nda-standard-clauses.md
│   └── liability-clause-fix.md
├── supplier-issue/
│   └── delayed-delivery-workaround.md
└── memory.db          ← SQLite FTS5 index
```

Each file looks like:
```markdown
---
id: a1b2c3d4e5f67890
topic: contract-review
title: NDA Standard Clauses
created_at: 2026-08-20T15:00:00Z
updated_at: 2026-08-20T15:00:00Z
---

# NDA Standard Clauses

The standard NDA should always include...
```

You can browse, edit, or delete memories directly — they're just Markdown files. The SQLite index stays in sync automatically.

## Local Development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export MEMORY_API_KEY=test-key
export MEMORY_DATA_DIR=./data/memories
python -m uvicorn src.server:app --host 0.0.0.0 --port 8080
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check (no auth required) |
| `/sse` | GET | SSE endpoint for MCP client connection |
| `/messages/` | POST | MCP message endpoint (used by SSE transport) |

## Tech Stack

- **Python 3.12** + [MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
- **Starlette** + **Uvicorn** for HTTP/SSE
- **SQLite FTS5** for full-text search (zero external dependencies)
- **Markdown** for human-readable storage

## License

MIT