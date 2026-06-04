"""
Session handoff — emit, verify, resume.

Philosophy (from the brief):
  - Facts from git, narrative from the model. git state is computed via
    subprocess; only the interpretive sections come from the caller.
  - Verify on ingest. Before a resuming session acts on a handoff, diff
    its claims against live repo state and surface a trust signal.
  - Automatic beats manual. PreCompact hook triggers auto-emit; the
    manual `handoff emit` command is a convenience.

Artifact format: Markdown with YAML frontmatter.
Storage: ~/.claude-compress/handoffs/<id>.md  (or $CLAUDE_COMPRESS_DIR)
"""

import datetime
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import store

HANDOFF_VERSION = 1
_INJECT_START = "<!-- claude-compress-handoff-start -->"
_INJECT_END = "<!-- claude-compress-handoff-end -->"

BODY_TEMPLATE = """\
## Where this stands
{where_this_stands}

## Decisions made
{decisions}

## Open threads / blockers
{blockers}

## Next concrete step
{next_step}

## Don't redo
{dont_redo}"""


# ── Directory & file helpers ──────────────────────────────────────────────

def _handoff_dir() -> Path:
    base = Path(os.environ.get("CLAUDE_COMPRESS_DIR", Path.home() / ".claude-compress"))
    d = base / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_id(handoff_id: str) -> Optional[Path]:
    """Resolve 'latest', an exact ID, or a partial prefix to a Path."""
    d = _handoff_dir()
    if handoff_id == "latest":
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None
    exact = d / f"{handoff_id}.md"
    if exact.exists():
        return exact
    matches = sorted(d.glob(f"*{handoff_id}*.md"))
    return matches[0] if matches else None


# ── Git helpers (all via subprocess, no GitPython) ────────────────────────

def _run_git(*args, cwd=None) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _repo_root() -> Optional[Path]:
    out = _run_git("rev-parse", "--show-toplevel")
    return Path(out) if out else None


def _git_head() -> Optional[str]:
    return _run_git("rev-parse", "HEAD")


def _git_branch() -> str:
    out = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    return out or "DETACHED"


def _git_dirty() -> bool:
    out = _run_git("status", "--porcelain")
    return bool(out)


def _file_sha256(path: Path) -> str:
    try:
        h = hashlib.sha256(path.read_bytes())
        return h.hexdigest()[:16]
    except OSError:
        return "unreadable"


def _decode_porcelain_status(xy: str) -> str:
    code = (xy.strip() or "?")[0].upper()
    return {
        "M": "modified", "A": "added", "D": "deleted",
        "R": "renamed", "C": "copied", "U": "unmerged",
        "?": "untracked",
    }.get(code, "modified")


def _git_touched_files() -> list:
    """
    Compute files_touched from git status --porcelain.
    Each entry: {path, status, sha256}.  sha256 is hashed now (emit time).
    """
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        out = r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        out = ""
    if not out.strip():
        return []

    repo = _repo_root()
    touched = {}

    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        raw_path = line[3:].strip()
        # Renames: "old -> new"
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ")[-1].strip()
        # Strip quotes git sometimes adds
        raw_path = raw_path.strip('"')
        status = _decode_porcelain_status(xy)
        touched[raw_path] = status

    result = []
    for rel, status in touched.items():
        full = (repo / rel) if repo else Path(rel)
        sha = _file_sha256(full) if full.exists() else "deleted"
        result.append({"path": rel, "status": status, "sha256": sha})

    return result


# ── Frontmatter serialisation / deserialisation (no PyYAML) ──────────────

def _fmt_frontmatter(meta: dict) -> str:
    lines = [
        "---",
        f"clawc_handoff_version: {meta['clawc_handoff_version']}",
        f"id: {meta['id']}",
        f"created_at: {meta['created_at']}",
        f"source_agent: {meta['source_agent']}",
        f"repo_root: {meta['repo_root']}",
        f"branch: {meta['branch']}",
        f"commit: {meta['commit']}",
        f"working_tree: {meta['working_tree']}",
        "files_touched:",
    ]
    for f in meta["files_touched"]:
        lines.append(
            f"  - {{path: {f['path']}, status: {f['status']}, sha256: {f['sha256']}}}"
        )
    lines += [
        f"task: \"{meta['task']}\"",
        f"status: {meta['status']}",
        "---",
    ]
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple:
    """
    Parse YAML-ish frontmatter without PyYAML.
    Returns (meta_dict, body_str).
    """
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---\n", 3)
    if end == -1:
        return {}, text

    fm_text = text[4:end]
    body = text[end + 5:]

    meta: dict = {}
    files: list = []
    in_files = False

    for line in fm_text.splitlines():
        if line.startswith("files_touched:"):
            in_files = True
            continue

        if in_files:
            stripped = line.lstrip()
            if stripped.startswith("- "):
                inner = stripped[2:].strip().lstrip("{").rstrip("}")
                entry: dict = {}
                for part in inner.split(", "):
                    if ": " in part:
                        k, v = part.split(": ", 1)
                        entry[k.strip()] = v.strip()
                files.append(entry)
                continue
            if not line.startswith(" ") and not line.startswith("\t"):
                in_files = False

        if ": " in line and not in_files:
            k, v = line.split(": ", 1)
            meta[k.strip()] = v.strip().strip('"')

    meta["files_touched"] = files
    return meta, body


def _make_id(branch: str, commit: str) -> str:
    date = datetime.date.today().isoformat()
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:30]
    short = (commit or "unknown")[:7]
    return f"{date}-{slug}-{short}"


# ── Public API ────────────────────────────────────────────────────────────

def emit(
    body: str,
    task: str = "",
    status: str = "in_progress",
) -> Path:
    """
    Emit a handoff artifact.

    Git facts (branch, commit, dirty state, files_touched + sha256) are
    computed right now via subprocess — never recalled from memory.
    The body (narrative) is provided by the caller.

    Returns the path of the written file.
    """
    commit = _git_head() or "unknown"
    branch = _git_branch()
    dirty = _git_dirty()
    touched = _git_touched_files()
    repo_root = _repo_root()

    base_id = _make_id(branch, commit)
    handoff_id = base_id
    d = _handoff_dir()
    out = d / f"{handoff_id}.md"
    counter = 1
    while out.exists():
        handoff_id = f"{base_id}-{counter}"
        out = d / f"{handoff_id}.md"
        counter += 1

    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    meta = {
        "clawc_handoff_version": HANDOFF_VERSION,
        "id": handoff_id,
        "created_at": now,
        "source_agent": "claude-code",
        "repo_root": str(repo_root or os.getcwd()),
        "branch": branch,
        "commit": commit[:7],
        "working_tree": "dirty" if dirty else "clean",
        "files_touched": touched,
        "task": task or "session capture",
        "status": status,
    }

    content = _fmt_frontmatter(meta) + "\n\n" + body.strip() + "\n"
    out.write_text(content, encoding="utf-8")
    return out


def verify(handoff_id: str) -> dict:
    """
    Reconcile a handoff's claims against live git state.

    Returns:
        signal  : "GREEN" | "YELLOW" | "RED"
        delta   : list of human-readable drift descriptions
        meta    : parsed frontmatter dict
    """
    path = _resolve_id(handoff_id)
    if not path:
        return {
            "signal": "RED",
            "delta": [f"Handoff '{handoff_id}' not found."],
            "meta": {},
        }

    meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    delta: list = []
    recorded_commit = meta.get("commit", "")

    # 1. Does the commit still exist?
    commit_type = _run_git("cat-file", "-t", recorded_commit)
    if not commit_type:
        delta.append(
            f"Commit {recorded_commit} not found — branch may have been force-pushed or rebased."
        )
        return {"signal": "RED", "delta": delta, "meta": meta}

    # 2. Has the branch advanced since?
    since_raw = _run_git("log", "--oneline", f"{recorded_commit}..HEAD")
    commits_since = [l for l in (since_raw or "").splitlines() if l]
    if commits_since:
        short_hashes = ", ".join(c.split()[0] for c in commits_since[:5])
        suffix = f" (+{len(commits_since) - 5} more)" if len(commits_since) > 5 else ""
        delta.append(
            f"Branch advanced by {len(commits_since)} commit(s) since handoff: {short_hashes}{suffix}"
        )

    # 3. Branch switched?
    current_branch = _git_branch()
    recorded_branch = meta.get("branch", "")
    if current_branch != recorded_branch:
        delta.append(
            f"Branch changed: was '{recorded_branch}', now '{current_branch}'."
        )

    # 4. Per-file hash drift
    repo_root = _repo_root()
    for f in meta.get("files_touched", []):
        rel = f.get("path", "")
        recorded_sha = f.get("sha256", "")
        full = (repo_root / rel) if repo_root else Path(rel)

        if recorded_sha == "deleted":
            if full.exists():
                delta.append(f"{rel}: was deleted at handoff time, now exists again.")
        elif not full.exists():
            if recorded_sha not in ("deleted", "unreadable"):
                delta.append(f"{rel}: deleted since handoff.")
        else:
            current_sha = _file_sha256(full)
            if current_sha != recorded_sha:
                delta.append(f"{rel}: modified since handoff (hash changed).")

    # Determine trust signal
    if any("not found" in d or "force-pushed" in d for d in delta):
        signal = "RED"
    elif delta:
        signal = "YELLOW"
    else:
        signal = "GREEN"

    return {"signal": signal, "delta": delta, "meta": meta}


def resume(handoff_id: str, inject_path: Optional[Path] = None) -> dict:
    """
    Verify a handoff then inject it into .claude/CLAUDE.md so the next
    Claude Code session picks it up automatically.

    The injected block is:
      - trust signal + drift delta (first, so the agent calibrates)
      - then the full handoff body

    Returns the verify result dict with 'injected_to' added.
    """
    result = verify(handoff_id)
    signal = result["signal"]
    meta = result["meta"]
    delta = result["delta"]

    path = _resolve_id(handoff_id)
    if not path:
        return result

    _, body = _parse_frontmatter(path.read_text(encoding="utf-8"))

    _SIGNAL_DESC = {
        "GREEN": "No drift — safe to act on this handoff.",
        "YELLOW": "Drift detected — review the delta before acting.",
        "RED":    "Significant drift — verify carefully before trusting.",
    }

    header_lines = [
        _INJECT_START,
        f"# Resumed handoff: {meta.get('id', handoff_id)}",
        "",
        f"**Trust: {signal}** — {_SIGNAL_DESC.get(signal, '')}",
    ]

    if delta:
        header_lines += ["", "**What changed since this handoff was emitted:**"]
        header_lines += [f"- {d}" for d in delta]

    header_lines += ["", "---", ""]
    injection = "\n".join(header_lines) + body.strip() + "\n" + _INJECT_END + "\n"

    # Locate .claude/CLAUDE.md
    if inject_path is None:
        repo_root = _repo_root()
        base = repo_root if repo_root else Path.cwd()
        inject_path = base / ".claude" / "CLAUDE.md"

    inject_path.parent.mkdir(parents=True, exist_ok=True)
    existing = inject_path.read_text(encoding="utf-8") if inject_path.exists() else ""
    # Remove any prior injection so re-running is idempotent
    existing = _strip_injection(existing)
    inject_path.write_text(injection + "\n" + existing, encoding="utf-8")

    result["injected_to"] = str(inject_path)
    return result


def clear_resume(inject_path: Optional[Path] = None) -> bool:
    """Remove a previously injected handoff from CLAUDE.md."""
    if inject_path is None:
        repo_root = _repo_root()
        base = repo_root if repo_root else Path.cwd()
        inject_path = base / ".claude" / "CLAUDE.md"

    if not inject_path.exists():
        return False

    content = inject_path.read_text(encoding="utf-8")
    new = _strip_injection(content)
    if new != content:
        inject_path.write_text(new, encoding="utf-8")
        return True
    return False


def _strip_injection(text: str) -> str:
    return re.sub(
        re.escape(_INJECT_START) + r".*?" + re.escape(_INJECT_END) + r"\n?",
        "",
        text,
        flags=re.DOTALL,
    ).lstrip("\n")


def list_handoffs() -> list:
    """Return handoff Paths sorted newest-first."""
    d = _handoff_dir()
    return sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def auto_emit() -> Optional[Path]:
    """
    Called from the PreCompact hook.  Builds a minimal body from session
    logs (no model involvement) and emits a stub handoff.  The engineer
    or next session can annotate it with `handoff emit --body`.
    """
    stats = store.compression_stats(since_hours=24)
    n = stats["compressions"]
    saved = stats["tokens_saved"]

    body = BODY_TEMPLATE.format(
        where_this_stands=(
            "_Auto-emitted at context compaction. "
            "Run `claude-compress handoff emit --body '...'` to add narrative._"
        ),
        decisions="- _Not recorded (auto-emit — annotate manually)_",
        blockers="- _Not recorded (auto-emit — annotate manually)_",
        next_step="- Review `git diff` and `git log --oneline -5`, then continue.",
        dont_redo=(
            f"- _Not recorded. Session: {n} commands compressed, {saved:,} tokens saved._"
        ),
    )

    try:
        return emit(body=body, task="auto-emitted at compaction")
    except Exception as e:
        print(f"[claude-compress] handoff auto-emit failed: {e}", file=sys.stderr)
        return None
