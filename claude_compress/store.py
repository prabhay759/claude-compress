"""
SQLite-backed store for dedup cache, session stats, and known-files tracking.
Database lives at ~/.claude-compress/cache.db.
"""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Dict


def _db_path() -> Path:
    base = Path(os.environ.get("CLAUDE_COMPRESS_DIR", Path.home() / ".claude-compress"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "cache.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dedup_cache (
            hash            TEXT PRIMARY KEY,
            compressed      TEXT NOT NULL,
            original_tokens INTEGER NOT NULL DEFAULT 0,
            compressed_tokens INTEGER NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL,
            stale           INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project         TEXT,
            cmd             TEXT,
            original_tokens INTEGER NOT NULL DEFAULT 0,
            compressed_tokens INTEGER NOT NULL DEFAULT 0,
            tags            TEXT,
            techniques      TEXT DEFAULT '',
            ts              INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS known_files (
            path            TEXT PRIMARY KEY,
            added_at        INTEGER NOT NULL
        );
    """)
    # Migrate existing DB: add techniques column if missing
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN techniques TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # column already exists
    conn.commit()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


# ── Dedup cache ───────────────────────────────────────────────────────────

def check_dedup(content: str) -> Optional[str]:
    """Return `§ref:HASH§` if content was seen before and is not stale, else None."""
    h = _sha256(content.encode())
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT hash FROM dedup_cache WHERE hash=? AND stale=0", (h,)
            ).fetchone()
            if row:
                return f"§ref:{h}§"
    except Exception:
        pass
    return None


def store_compressed(content: str, compressed: str) -> None:
    """Persist a compression result for future dedup hits."""
    h = _sha256(content.encode())
    orig_tokens = _estimate_tokens(content)
    comp_tokens = _estimate_tokens(compressed)
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO dedup_cache
                   (hash, compressed, original_tokens, compressed_tokens, created_at, stale)
                   VALUES (?,?,?,?,?,0)""",
                (h, compressed, orig_tokens, comp_tokens, int(time.time())),
            )
            conn.commit()
    except Exception:
        pass


def mark_all_stale() -> None:
    """Called on PreCompact — marks all dedup entries stale so the new session re-compresses."""
    try:
        with _connect() as conn:
            conn.execute("UPDATE dedup_cache SET stale=1")
            conn.commit()
    except Exception:
        pass


def mark_all_fresh() -> None:
    """Called on SessionStart/resume — re-activates dedup entries for the new session."""
    try:
        with _connect() as conn:
            conn.execute("UPDATE dedup_cache SET stale=0")
            conn.commit()
    except Exception:
        pass


# ── Session log ───────────────────────────────────────────────────────────

def log_compression(cmd: str, original_tokens: int, compressed_tokens: int,
                    tags: Optional[list] = None,
                    techniques: Optional[List[Dict]] = None) -> None:
    project = _current_project()
    tag_str = ",".join(tags) if tags else ""
    techniques_json = json.dumps(techniques) if techniques else ""
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO sessions
                   (project, cmd, original_tokens, compressed_tokens, tags, techniques, ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (project, cmd, original_tokens, compressed_tokens,
                 tag_str, techniques_json, int(time.time())),
            )
            conn.commit()
    except Exception:
        pass


def last_compressions(n: int = 5) -> List[Dict]:
    """Return the last N compression records with technique breakdowns."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT cmd, original_tokens, compressed_tokens, techniques, ts
                   FROM sessions ORDER BY ts DESC LIMIT ?""",
                (n,),
            ).fetchall()
        result = []
        for cmd, orig, comp, tech_json, ts in rows:
            techniques = []
            if tech_json:
                try:
                    techniques = json.loads(tech_json)
                except Exception:
                    pass
            result.append({
                "cmd": cmd or "",
                "original_tokens": orig,
                "compressed_tokens": comp,
                "techniques": techniques,
                "ts": ts,
            })
        return result
    except Exception:
        return []


def compression_stats(since_hours: int = 24) -> dict:
    """Return aggregate stats for the last `since_hours` hours."""
    cutoff = int(time.time()) - since_hours * 3600
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(original_tokens), SUM(compressed_tokens)
                   FROM sessions WHERE ts >= ?""",
                (cutoff,),
            ).fetchone()
            count, orig, comp = row if row else (0, 0, 0)
            orig = orig or 0
            comp = comp or 0
            saved = orig - comp
            pct = round(saved / orig * 100, 1) if orig > 0 else 0.0
            return {
                "compressions": count or 0,
                "original_tokens": orig,
                "compressed_tokens": comp,
                "tokens_saved": saved,
                "reduction_pct": pct,
            }
    except Exception:
        return {"compressions": 0, "original_tokens": 0, "compressed_tokens": 0,
                "tokens_saved": 0, "reduction_pct": 0.0}


# ── Known files ───────────────────────────────────────────────────────────

def add_known_file(path: str) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO known_files (path, added_at) VALUES (?,?)",
                (path, int(time.time())),
            )
            conn.commit()
    except Exception:
        pass


def known_files() -> list:
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT path FROM known_files").fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


# ── Helpers ───────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _current_project() -> Optional[str]:
    try:
        return os.getcwd()
    except Exception:
        return None
