"""
Core compression engine for claude-compress.

Pipeline (in order):
1. ANSI strip
2. Dedup check  → §ref:HASH§ on hit
3. Secret detection → safe mode (skip caching, light compression only)
4. Command-specific formatters (git, cargo, pytest, json)
5. Generic passes (RLE, blank-line collapse, repeated-line collapse)
6. Truncation for very large outputs
7. Store in dedup cache
8. Fallback: return original on any exception
"""

import json
import re
import sys
from typing import List, Dict, Optional, Tuple

from . import store

# ── Constants ─────────────────────────────────────────────────────────────

MAX_LINES = 500          # lines above which we truncate
KEEP_HEAD = 200          # lines to keep from the top when truncating
KEEP_TAIL = 50           # lines to keep from the bottom when truncating
MAX_BYTES = 500_000      # hard byte limit before we truncate

# Secret patterns — content matching these skips the dedup cache
_SECRET_RE = re.compile(
    r"""(
        (?:api[_-]?key|secret|token|password|passwd|auth)[^\n]{0,4}[:=]\s*['"]?[A-Za-z0-9+/=_\-]{16,}
        | sk-[A-Za-z0-9]{32,}
        | gh[pousr]_[A-Za-z0-9]{36,}
        | AKIA[A-Z0-9]{16}
        | eyJ[A-Za-z0-9_\-]{20,}
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# ANSI escape sequences
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")


# ── Public API ────────────────────────────────────────────────────────────

def compress(text: str, cmd: str = "") -> str:
    """Compress `text` produced by `cmd`. Never raises — returns original on failure."""
    return compress_explained(text, cmd)[0]


def compress_explained(text: str, cmd: str = "") -> Tuple[str, List[Dict]]:
    """
    Compress text and return (compressed_text, techniques).

    techniques is a list of {"name": str, "saved": int} dicts describing
    what each pipeline step saved. Empty list means no compression occurred.
    Never raises.
    """
    if not text or not text.strip():
        return text, []
    try:
        return _compress_tracked(text, cmd)
    except Exception as e:
        print(f"[claude-compress] fallback: {e}", file=sys.stderr)
        return text, []


# ── Internal pipeline ─────────────────────────────────────────────────────

def _compress_tracked(text: str, cmd: str) -> Tuple[str, List[Dict]]:
    techniques: List[Dict] = []
    current = text

    def tok(s: str) -> int:
        return store._estimate_tokens(s)

    def record(name: str, before: str, after: str) -> str:
        saved = tok(before) - tok(after)
        if saved > 0:
            techniques.append({"name": name, "saved": saved})
        return after

    # 1. Hard byte limit (binary / enormous output)
    if len(current.encode()) > MAX_BYTES:
        lines = current.splitlines(keepends=True)
        n = len(lines)
        truncated = (
            "".join(lines[:KEEP_HEAD])
            + f"\n[... {n - KEEP_HEAD - KEEP_TAIL} lines truncated]\n"
            + "".join(lines[-KEEP_TAIL:])
        )
        current = record("truncate", current, truncated)

    # 2. ANSI strip
    current = record("ansi", current, _ANSI_RE.sub("", current))
    ansi_stripped = current  # reference point for dedup key and store threshold

    # 3. Dedup check (use ANSI-stripped content as key)
    ref = store.check_dedup(current)
    if ref:
        techniques.append({"name": "dedup", "saved": tok(current) - tok(ref)})
        return ref, techniques

    # 4. Secret detection → safe mode
    safe_mode = bool(_SECRET_RE.search(current))

    # 5. Command-specific formatter
    base_cmd = _base_cmd(cmd)
    fmt_name = _formatter_name(base_cmd)
    current = record(fmt_name, current, _apply_formatter(base_cmd, current))

    # 6. Blank line collapse
    current = record("blank", current, _collapse_blank_lines(current))

    # 7. RLE — repeated line collapse
    current = record("rle", current, _collapse_repeated_lines(current))

    # 8. Line truncation
    lines = current.splitlines()
    if len(lines) > MAX_LINES:
        n = len(lines)
        kept = (
            lines[:KEEP_HEAD]
            + [f"[... {n - KEEP_HEAD - KEEP_TAIL} lines truncated]"]
            + lines[-KEEP_TAIL:]
        )
        current = record("truncate", current, "\n".join(kept))

    # 9. Store if beneficial and no secrets
    if not safe_mode and len(current) < len(ansi_stripped):
        store.store_compressed(ansi_stripped, current)

    # 10. Annotate known-file refs
    current = _apply_context_refs(current)

    # 11. Return ANSI-stripped original if compression made no improvement
    if len(current) >= len(ansi_stripped):
        return ansi_stripped, []

    return current, techniques


def _formatter_name(base_cmd: str) -> str:
    if base_cmd in ("git",):
        return "git"
    if base_cmd in ("cargo", "rustc"):
        return "cargo"
    if base_cmd in ("pytest", "python", "python3"):
        return "pytest"
    if base_cmd in ("npm", "yarn", "pnpm", "bun"):
        return "node"
    return "json"


# ── ANSI & whitespace ─────────────────────────────────────────────────────

def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def _collapse_repeated_lines(text: str) -> str:
    """Collapse runs of identical lines: 'X\nX\nX' → 'X (×3)'."""
    lines = text.splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        j = i + 1
        stripped = lines[i].rstrip("\n")
        while j < len(lines) and lines[j].rstrip("\n") == stripped:
            j += 1
        count = j - i
        if count >= 3:
            out.append(f"{stripped} (\xd7{count})\n")
        else:
            out.extend(lines[i:j])
        i = j
    return "".join(out)


# ── Command-specific formatters ───────────────────────────────────────────

def _apply_formatter(base_cmd: str, text: str) -> str:
    if base_cmd in ("git",):
        return _fmt_git(text)
    if base_cmd in ("cargo", "rustc"):
        return _fmt_cargo(text)
    if base_cmd in ("pytest", "python", "python3"):
        return _fmt_pytest(text)
    if base_cmd in ("npm", "yarn", "pnpm", "bun"):
        return _fmt_node(text)
    # Try JSON minification regardless of command
    return _try_json_minify(text)


def _fmt_git(text: str) -> str:
    """Fold unchanged diff hunks; keep added/removed lines."""
    lines = text.splitlines(keepends=True)
    out = []
    context_run: list = []

    def flush_context():
        if len(context_run) > 3:
            out.append(context_run[0])
            out.append(f"  ... ({len(context_run) - 2} unchanged lines)\n")
            out.append(context_run[-1])
        else:
            out.extend(context_run)
        context_run.clear()

    for line in lines:
        if line.startswith((" ", "\t")) and not line.startswith("--- ") and not line.startswith("+++ "):
            context_run.append(line)
        else:
            flush_context()
            out.append(line)
    flush_context()
    return "".join(out)


def _fmt_cargo(text: str) -> str:
    """Keep errors/warnings; fold repetitive 'Compiling X' lines."""
    lines = text.splitlines(keepends=True)
    compiling = 0
    out = []
    for line in lines:
        if line.startswith("   Compiling "):
            compiling += 1
        else:
            if compiling > 0:
                out.append(f"   Compiling ... ({compiling} crates)\n")
                compiling = 0
            out.append(line)
    if compiling:
        out.append(f"   Compiling ... ({compiling} crates)\n")
    return "".join(out)


def _fmt_pytest(text: str) -> str:
    """Keep failures and summary; fold passing test lines."""
    lines = text.splitlines(keepends=True)
    out = []
    in_failure = False
    passed_count = 0

    for line in lines:
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            in_failure = True
            if passed_count:
                out.append(f"[{passed_count} passed]\n")
                passed_count = 0
            out.append(line)
        elif line.startswith("PASSED "):
            passed_count += 1
        elif line.startswith("=====") or line.startswith("_____"):
            in_failure = True
            if passed_count:
                out.append(f"[{passed_count} passed]\n")
                passed_count = 0
            out.append(line)
        elif line.startswith("short test summary"):
            in_failure = True
            if passed_count:
                out.append(f"[{passed_count} passed]\n")
                passed_count = 0
            out.append(line)
        else:
            if in_failure:
                out.append(line)
            elif line.strip().startswith("collected") or "passed" in line or "failed" in line:
                out.append(line)
            elif line.strip() == "" or line.startswith("platform") or line.startswith("rootdir"):
                out.append(line)
            else:
                passed_count += 1

    if passed_count:
        out.append(f"[{passed_count} lines omitted]\n")
    return "".join(out)


def _fmt_node(text: str) -> str:
    """Fold verbose npm install output; keep errors."""
    lines = text.splitlines(keepends=True)
    npm_lines = 0
    out = []
    for line in lines:
        if re.match(r"^(npm WARN|added \d|packages? are|found \d)", line):
            npm_lines += 1
        else:
            if npm_lines:
                out.append(f"[npm: {npm_lines} info lines omitted]\n")
                npm_lines = 0
            out.append(line)
    if npm_lines:
        out.append(f"[npm: {npm_lines} info lines omitted]\n")
    return "".join(out)


def _try_json_minify(text: str) -> str:
    """If the entire output is JSON, strip null fields and compact it."""
    stripped = text.strip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return text
    try:
        obj = json.loads(stripped)
        cleaned = _strip_nulls(obj)
        minified = json.dumps(cleaned, separators=(",", ":"), ensure_ascii=False)
        # Only use if materially smaller
        return minified + "\n" if len(minified) < len(stripped) * 0.9 else text
    except (json.JSONDecodeError, ValueError):
        return text


def _strip_nulls(obj):
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj if v is not None]
    return obj


# ── Context reference annotations ────────────────────────────────────────

def _apply_context_refs(text: str) -> str:
    """Annotate file paths already known to this session with [in context]."""
    known = store.known_files()
    if not known:
        return text
    for path in known:
        marker = f"--> {path}"
        if marker in text:
            text = text.replace(marker, f"{marker} [in context]")
        at_marker = f"at {path}:"
        if at_marker in text:
            text = text.replace(at_marker, f"at {path} [in context]:")
    return text


# ── Helpers ───────────────────────────────────────────────────────────────

def _base_cmd(cmd: str) -> str:
    """Extract the base command name from a full command string."""
    if not cmd:
        return ""
    first = cmd.strip().split()[0]
    return first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
