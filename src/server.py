"""
MCP Memory Server — persistent agent memory via Markdown + SQLite FTS5.

Provides three tools for Blockbrain (or any MCP client):
  - memory_save: Store a memory entry under a topic
  - memory_recall: Search memories by keyword/topic
  - memory_list_topics: List all stored topics

Storage: Markdown files on disk + SQLite FTS5 index for fast search.
Transport: SSE (Server-Sent Events) — what Blockbrain expects.
Auth: API Key header (X-API-Key).
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
import uvicorn


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("MEMORY_DATA_DIR", "/data/memories"))
DB_PATH = DATA_DIR / "memory.db"
API_KEY = os.environ.get("MEMORY_API_KEY", "")
PORT = int(os.environ.get("MEMORY_PORT", "8080"))
MAX_QUERY_RESULTS = int(os.environ.get("MEMORY_MAX_RESULTS", "10"))

DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Create a filesystem-safe slug from text."""
    slug = re.sub(r"[^a-zA-Z0-9äöüßéèêàáíóúñ\- ]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug.strip())
    return slug[:80] or "untitled"


def _init_db() -> sqlite3.Connection:
    """Initialize SQLite with FTS5 full-text search."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            file_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # FTS5 virtual table for full-text search
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
        USING fts5(topic, title, content, content='memories', content_rowid='rowid')
    """)
    # Triggers to keep FTS in sync
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, topic, title, content)
            VALUES (new.rowid, new.topic, new.title, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, topic, title, content)
            VALUES ('delete', old.rowid, old.topic, old.title, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, topic, title, content)
            VALUES ('delete', old.rowid, old.topic, old.title, old.content);
            INSERT INTO memories_fts(rowid, topic, title, content)
            VALUES (new.rowid, new.topic, new.title, new.content);
        END
    """)
    conn.commit()
    return conn


DB = _init_db()


def save_memory(topic: str, title: str, content: str) -> dict:
    """Save a memory entry as Markdown + index in SQLite."""
    topic_slug = _slug(topic)
    topic_dir = DATA_DIR / topic_slug
    topic_dir.mkdir(parents=True, exist_ok=True)

    # Generate ID from topic+title hash for idempotency
    entry_id = hashlib.sha256(f"{topic}:{title}".encode()).hexdigest()[:16]
    now = datetime.now(timezone.utc).isoformat()
    md_filename = f"{_slug(title)}.md"
    md_path = topic_dir / md_filename

    # Write Markdown file
    md_content = f"""---
id: {entry_id}
topic: {topic}
title: {title}
created_at: {now}
updated_at: {now}
---

# {title}

{content}
"""
    md_path.write_text(md_content, encoding="utf-8")

    # Upsert into SQLite
    DB.execute("""
        INSERT INTO memories (id, topic, title, content, file_path, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            content = excluded.content,
            file_path = excluded.file_path,
            updated_at = excluded.updated_at
    """, (entry_id, topic, title, content, str(md_path), now, now))
    DB.commit()

    return {
        "id": entry_id,
        "topic": topic,
        "title": title,
        "file": str(md_path),
        "status": "saved"
    }


def recall_memory(query: str, limit: int = MAX_QUERY_RESULTS) -> dict:
    """Search memories using SQLite FTS5 full-text search."""
    # FTS5 query syntax: escape special chars, use OR for multi-word
    safe_query = re.sub(r'[()*":]', " ", query).strip()
    if not safe_query:
        return {"results": [], "count": 0}

    # Try FTS5 MATCH first; fall back to LIKE if FTS finds nothing
    rows = DB.execute("""
        SELECT m.id, m.topic, m.title, m.content, m.file_path, m.created_at,
               rank
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE memories_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (safe_query, limit)).fetchall()

    if not rows:
        # Fallback: LIKE search
        pattern = f"%{query}%"
        rows = DB.execute("""
            SELECT id, topic, title, content, file_path, created_at, 0 as rank
            FROM memories
            WHERE topic LIKE ? OR title LIKE ? OR content LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (pattern, pattern, pattern, limit)).fetchall()

    results = []
    for row in rows:
        results.append({
            "id": row[0],
            "topic": row[1],
            "title": row[2],
            "content": row[3][:500] + ("..." if len(row[3]) > 500 else ""),
            "file": row[4],
            "created_at": row[5],
        })

    return {"results": results, "count": len(results)}


def list_topics() -> dict:
    """List all topics that have stored memories."""
    rows = DB.execute("""
        SELECT topic, COUNT(*) as count, MIN(created_at) as first, MAX(updated_at) as last
        FROM memories
        GROUP BY topic
        ORDER BY last DESC
    """).fetchall()

    topics = []
    for row in rows:
        topics.append({
            "topic": row[0],
            "entries": row[1],
            "first_entry": row[2],
            "last_entry": row[3],
        })

    return {"topics": topics, "total": len(topics)}


def delete_memory(entry_id: str) -> dict:
    """Delete a memory entry by ID."""
    row = DB.execute("SELECT file_path FROM memories WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        return {"id": entry_id, "status": "not_found"}

    file_path = Path(row[0])
    DB.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
    DB.commit()

    if file_path.exists():
        file_path.unlink()

    return {"id": entry_id, "status": "deleted"}


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("memory-server")


@server.list_tools()
async def list_tools():
    from mcp.types import Tool
    return [
        Tool(
            name="memory_save",
            description=(
                "Save a memory entry so the agent can recall it in future sessions. "
                "Use this AFTER solving a problem, completing a task, or learning "
                "something worth remembering. Always provide a clear topic and title."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Category/topic for this memory (e.g. 'contract-review', 'supplier-issue')",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short descriptive title for this memory entry",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to remember — what was the problem, what was the solution, what was learned",
                    },
                },
                "required": ["topic", "title", "content"],
            },
        ),
        Tool(
            name="memory_recall",
            description=(
                "Search stored memories by keyword or topic. "
                "Use this BEFORE starting a task to check if similar problems "
                "were solved before and what the solution was."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — keywords, topic name, or problem description",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results (default 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_list_topics",
            description=(
                "List all topics that have stored memories. "
                "Use this to get an overview of what the agent already knows."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="memory_delete",
            description=(
                "Delete a specific memory entry by its ID. "
                "Use this to remove outdated or incorrect memories."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The ID of the memory entry to delete",
                    },
                },
                "required": ["id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    from mcp.types import TextContent

    if name == "memory_save":
        result = save_memory(
            topic=arguments["topic"],
            title=arguments["title"],
            content=arguments["content"],
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "memory_recall":
        result = recall_memory(
            query=arguments["query"],
            limit=arguments.get("limit", MAX_QUERY_RESULTS),
        )
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "memory_list_topics":
        result = list_topics()
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "memory_delete":
        result = delete_memory(arguments["id"])
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# ---------------------------------------------------------------------------
# HTTP Server with SSE transport + API Key auth
# ---------------------------------------------------------------------------

class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Validate X-API-Key header if MEMORY_API_KEY is set."""

    async def dispatch(self, request: Request, call_next):
        if not API_KEY:
            return await call_next(request)

        # Health endpoint is always open
        if request.url.path == "/health":
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if provided != API_KEY:
            return JSONResponse(
                {"error": "Invalid or missing API key"},
                status_code=401,
            )
        return await call_next(request)


async def health(request: Request) -> Response:
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok",
        "tools": ["memory_save", "memory_recall", "memory_list_topics", "memory_delete"],
        "storage": str(DATA_DIR),
    })


def create_app() -> Starlette:
    """Create the Starlette app with SSE transport for MCP."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ]

    middleware = [Middleware(ApiKeyMiddleware)]

    return Starlette(routes=routes, middleware=middleware)


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "src.server:app",
        host="0.0.0.0",
        port=PORT,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )