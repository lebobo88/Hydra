"""Xenia KB — SQLite FTS5 RAG MCP server for the Xenia customer-support squad.

Exposes:
  xenia-kb.search(query, top_k=5, topic_class=null)
  xenia-kb.get(doc_id)
  xenia-kb.list()
  xenia-kb.ping

Index: SQLite FTS5 over section-level chunks of markdown files in
<XENIA root>/hearth/kb/*.md.  The index is stored at
<XENIA root>/hearth/kb/.kb-index.db and rebuilt lazily when any
source file is newer than the DB.

Staleness thresholds (from freshness-aware-retrieval SKILL.md):
  volatile  → stale after  90 days
  active    → stale after 180 days
  stable    → stale after 730 days

Stale volatile docs are still returned but flagged stale=True and
demoted below all fresh results (score multiplied by STALE_PENALTY).

Citation form enabled by result fields:
  [source: <doc_id> | <section> | <as_of_date>]
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from mcp_servers._pack_shim import resolve_root, run_server  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DB_NAME = ".kb-index.db"
_KB_SUBDIR = "hearth/kb"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_SPLIT_RE = re.compile(r"(?=^## )", re.MULTILINE)

# Staleness thresholds in days
_STALE_DAYS: dict[str, int] = {
    "volatile": 90,
    "active": 180,
    "stable": 730,
}

# Score multiplier for stale results (demotes them below fresh results)
_STALE_PENALTY = 0.01

# Number of characters for snippet context
_SNIPPET_CHARS = 300

# ---------------------------------------------------------------------------
# Frontmatter parsing (minimal YAML subset — no external deps)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter_dict, body_text).

    Handles only the simple key: value pairs used in KB articles.
    Does not handle YAML lists, nested structures, or quoted values with
    colons in them — sufficient for the known KB schema.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            # Strip surrounding quotes (single or double) from the value
            val = val.strip().strip('"').strip("'")
            fm[key.strip()] = val
    body = text[m.end():]
    return fm, body


# ---------------------------------------------------------------------------
# Staleness calculation
# ---------------------------------------------------------------------------

def _is_stale(as_of_str: str, topic_class: str, today: date | None = None) -> bool:
    """Return True if the document is past its freshness threshold."""
    if today is None:
        today = date.today()
    threshold_days = _STALE_DAYS.get(topic_class.lower(), 730)
    try:
        as_of = date.fromisoformat(as_of_str)
    except (ValueError, TypeError):
        # Unknown date → treat as oldest in its class (always stale)
        return True
    return (today - as_of) > timedelta(days=threshold_days)


# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------

def _chunk_document(
    doc_id: str,
    title: str,
    as_of: str,
    topic_class: str,
    owner: str,
    body: str,
) -> list[dict[str, str]]:
    """Split body on '## ' headings into section-level chunks.

    Returns a list of dicts with keys:
      doc_id, title, section, as_of_date, topic_class, owner, content
    """
    chunks: list[dict[str, str]] = []
    parts = _HEADING_SPLIT_RE.split(body)

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.startswith("## "):
            # Extract heading and content
            first_newline = part.find("\n")
            if first_newline == -1:
                section = part[3:].strip()
                content = ""
            else:
                section = part[3:first_newline].strip()
                content = part[first_newline + 1:].strip()
        else:
            # Text before the first ## heading (preamble / introduction)
            section = "Overview"
            content = part

        if content:
            chunks.append({
                "doc_id": doc_id,
                "title": title,
                "section": section,
                "as_of_date": as_of,
                "topic_class": topic_class,
                "owner": owner,
                "content": content,
            })

    return chunks


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

def _get_db(kb_dir: Path) -> sqlite3.Connection:
    """Open the FTS5 index database, creating it if absent."""
    db_path = kb_dir / _DB_NAME
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            doc_id UNINDEXED,
            title,
            section,
            as_of_date UNINDEXED,
            topic_class UNINDEXED,
            owner UNINDEXED,
            content,
            tokenize='porter unicode61'
        )
    """)
    conn.commit()


def _index_needs_rebuild(conn: sqlite3.Connection, md_files: list[Path]) -> bool:
    """Return True if the index is stale and must be rebuilt.

    Triggers a rebuild when ANY of:
      1. No 'indexed_at' timestamp is stored (fresh DB).
      2. Any source .md file has a mtime newer than the stored index timestamp
         (covers: doc updated, doc added).
      3. The count of source .md files differs from the count stored at last
         index time (covers: doc deleted — mtime-only check cannot detect this
         because no surviving file changes mtime when a peer is removed).
    """
    row = conn.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    if row is None:
        return True
    try:
        indexed_at = float(row["value"])
    except (ValueError, TypeError):
        return True

    # Check 3: file-count change (catches deletions that mtime alone misses)
    count_row = conn.execute("SELECT value FROM meta WHERE key='indexed_file_count'").fetchone()
    if count_row is not None:
        try:
            indexed_count = int(count_row["value"])
            if indexed_count != len(md_files):
                return True
        except (ValueError, TypeError):
            return True  # Can't parse stored count — rebuild to be safe
    # If no stored count (pre-existing DB without this meta key), fall through
    # to mtime check; the next rebuild will store the count.

    # Check 2: mtime change (covers updates and additions)
    for f in md_files:
        if f.stat().st_mtime > indexed_at:
            return True
    return False


def _rebuild_index(conn: sqlite3.Connection, kb_dir: Path, md_files: list[Path]) -> int:
    """Rebuild the FTS5 index from scratch. Returns the number of chunks indexed."""
    conn.execute("DELETE FROM chunks")

    total = 0
    for md_path in md_files:
        text = md_path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(text)

        doc_id = fm.get("doc_id") or md_path.stem
        title = fm.get("title") or doc_id
        as_of = fm.get("as_of") or ""
        topic_class = fm.get("topic_class") or "stable"
        owner = fm.get("owner") or "kb-team"

        for chunk in _chunk_document(doc_id, title, as_of, topic_class, owner, body):
            conn.execute(
                "INSERT INTO chunks(doc_id, title, section, as_of_date, topic_class, owner, content)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk["doc_id"],
                    chunk["title"],
                    chunk["section"],
                    chunk["as_of_date"],
                    chunk["topic_class"],
                    chunk["owner"],
                    chunk["content"],
                ),
            )
            total += 1

    import time
    now_ts = str(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_at', ?)",
        (now_ts,),
    )
    # Also persist the file count so _index_needs_rebuild can detect deletions.
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('indexed_file_count', ?)",
        (str(len(md_files)),),
    )
    conn.commit()
    return total


def _ensure_index(kb_dir: Path) -> tuple[sqlite3.Connection, bool]:
    """Open (and if necessary rebuild) the FTS5 index.

    Returns (conn, was_rebuilt).
    """
    md_files = sorted(kb_dir.glob("*.md"))
    conn = _get_db(kb_dir)
    _ensure_schema(conn)

    if _index_needs_rebuild(conn, md_files):
        _rebuild_index(conn, kb_dir, md_files)
        return conn, True
    return conn, False


# ---------------------------------------------------------------------------
# Snippet helper
# ---------------------------------------------------------------------------

def _make_snippet(content: str, query_terms: list[str]) -> str:
    """Return a short snippet from content, preferring the region with the
    highest density of query terms.  Falls back to the first N chars."""
    lower = content.lower()
    best_pos = 0
    best_hits = 0
    # Slide a window across the content
    window = _SNIPPET_CHARS
    step = max(1, window // 4)
    for i in range(0, max(1, len(content) - window), step):
        region = lower[i: i + window]
        hits = sum(1 for t in query_terms if t.lower() in region)
        if hits > best_hits:
            best_hits = hits
            best_pos = i

    raw = content[best_pos: best_pos + _SNIPPET_CHARS].strip()
    if best_pos > 0:
        raw = "…" + raw
    if best_pos + _SNIPPET_CHARS < len(content):
        raw = raw + "…"
    return raw


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _tool_handlers() -> dict[str, Any]:
    root = resolve_root("HYDRA_XENIA_ROOT", str(_HERE.parents[2].parent / "Xenia"))
    kb_dir = root / _KB_SUBDIR

    # ------------------------------------------------------------------ search
    def search(args: dict[str, Any]) -> dict[str, Any]:
        query: str = args.get("query", "").strip()
        if not query:
            return {"error": "query is required"}
        top_k: int = int(args.get("top_k", 5))
        topic_class_filter: str | None = args.get("topic_class") or None

        if not kb_dir.exists():
            return {"error": f"KB directory not found: {kb_dir}"}

        conn, _ = _ensure_index(kb_dir)
        today = date.today()

        # Build the FTS5 query; escape double quotes in the query string
        safe_query = query.replace('"', '""')
        sql = "SELECT doc_id, title, section, as_of_date, topic_class, owner, content, rank FROM chunks WHERE chunks MATCH ? ORDER BY rank"
        params: list[Any] = [safe_query]

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # FTS5 query syntax error — fall back to simple LIKE search
            like_pat = f"%{query}%"
            rows = conn.execute(
                "SELECT doc_id, title, section, as_of_date, topic_class, owner, content, 0 as rank FROM chunks WHERE content LIKE ? OR title LIKE ?",
                [like_pat, like_pat],
            ).fetchall()

        query_terms = re.findall(r"\w+", query)
        results: list[dict[str, Any]] = []
        for row in rows:
            tc = (row["topic_class"] or "stable").lower()
            if topic_class_filter and tc != topic_class_filter.lower():
                continue

            stale = _is_stale(row["as_of_date"] or "", tc, today)
            # FTS5 rank is negative (more negative = more relevant)
            # We want a positive score; negate the rank.
            base_score = -(row["rank"] or 0.0)
            score = base_score * _STALE_PENALTY if stale else base_score

            results.append({
                "doc_id": row["doc_id"],
                "title": row["title"],
                "section": row["section"],
                "as_of_date": row["as_of_date"],
                "topic_class": tc,
                "snippet": _make_snippet(row["content"] or "", query_terms),
                "score": round(score, 6),
                "stale": stale,
            })

        # Sort: fresh first (by descending score), then stale (by descending score)
        fresh = sorted([r for r in results if not r["stale"]], key=lambda r: r["score"], reverse=True)
        stale_results = sorted([r for r in results if r["stale"]], key=lambda r: r["score"], reverse=True)
        ordered = (fresh + stale_results)[:top_k]

        return {"results": ordered, "total_candidates": len(results), "query": query}

    # -------------------------------------------------------------------- get
    def get(args: dict[str, Any]) -> dict[str, Any]:
        doc_id: str = args.get("doc_id", "").strip()
        if not doc_id:
            return {"error": "doc_id is required"}

        # Look for a file whose stem matches doc_id (or whose frontmatter doc_id matches)
        if not kb_dir.exists():
            return {"error": f"KB directory not found: {kb_dir}"}

        # Try exact filename stem first
        candidate = kb_dir / f"{doc_id}.md"
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            fm, body = _parse_frontmatter(text)
            today = date.today()
            tc = (fm.get("topic_class") or "stable").lower()
            stale = _is_stale(fm.get("as_of") or "", tc, today)
            return {
                "doc_id": fm.get("doc_id") or doc_id,
                "title": fm.get("title") or doc_id,
                "as_of_date": fm.get("as_of") or "",
                "topic_class": tc,
                "owner": fm.get("owner") or "kb-team",
                "stale": stale,
                "content": text,
            }

        # Fall back: scan all .md files for matching frontmatter doc_id
        for md_path in kb_dir.glob("*.md"):
            text = md_path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)
            if fm.get("doc_id") == doc_id:
                today = date.today()
                tc = (fm.get("topic_class") or "stable").lower()
                stale = _is_stale(fm.get("as_of") or "", tc, today)
                return {
                    "doc_id": doc_id,
                    "title": fm.get("title") or doc_id,
                    "as_of_date": fm.get("as_of") or "",
                    "topic_class": tc,
                    "owner": fm.get("owner") or "kb-team",
                    "stale": stale,
                    "content": text,
                }

        return {"error": "not_found", "doc_id": doc_id}

    # ------------------------------------------------------------------- list
    def list_docs(args: dict[str, Any]) -> dict[str, Any]:
        if not kb_dir.exists():
            return {"docs": [], "error": f"KB directory not found: {kb_dir}"}

        today = date.today()
        docs: list[dict[str, Any]] = []
        for md_path in sorted(kb_dir.glob("*.md")):
            text = md_path.read_text(encoding="utf-8")
            fm, _ = _parse_frontmatter(text)
            tc = (fm.get("topic_class") or "stable").lower()
            stale = _is_stale(fm.get("as_of") or "", tc, today)
            docs.append({
                "doc_id": fm.get("doc_id") or md_path.stem,
                "title": fm.get("title") or md_path.stem,
                "as_of_date": fm.get("as_of") or "",
                "topic_class": tc,
                "stale": stale,
            })
        return {"docs": docs}

    # ------------------------------------------------------------------- ping
    def ping(args: dict[str, Any]) -> dict[str, Any]:
        if not kb_dir.exists():
            return {"ok": False, "root": str(root), "error": "KB directory not found"}

        md_files = sorted(kb_dir.glob("*.md"))
        doc_count = len(md_files)

        conn = _get_db(kb_dir)
        _ensure_schema(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
        index_fresh = row is not None and not _index_needs_rebuild(conn, md_files)

        return {
            "ok": True,
            "root": str(root),
            "kb_dir": str(kb_dir),
            "doc_count": doc_count,
            "index_fresh": index_fresh,
        }

    return {
        "xenia-kb.search": search,
        "xenia-kb.get": get,
        "xenia-kb.list": list_docs,
        "xenia-kb.ping": ping,
    }


def main() -> None:
    run_server("xenia-kb", _tool_handlers())
