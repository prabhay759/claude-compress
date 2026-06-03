"""
Claude Code PreToolUse / PreCompact hook handler.

Reads JSON from stdin, decides whether to rewrite the Bash command
to pipe through `claude-compress compress`, and writes the result
to stdout.  Returns `{}` (passthrough) for all miss/skip cases.

Claude Code hook JSON:
  Input:  {"tool_name": "Bash", "tool_input": {"command": "git status"}}
  Output: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": "...rewritten..."}}}
  Skip:   {}
"""

import json
import sys
from typing import Optional

from . import store

# ── Tool names that mean "run a shell command" ────────────────────────────
_BASH_TOOL_NAMES = {
    "bash", "shell", "terminal",
    "run_terminal_command", "run_shell_command", "execute_bash",
}

# ── Interactive commands — must NOT be wrapped ────────────────────────────
_INTERACTIVE_CMDS = {
    "vim", "vi", "nvim", "nano", "emacs", "pico",
    "less", "more", "man",
    "ssh", "telnet", "sftp", "ftp",
    "python", "python3", "ipython", "bpython",
    "node", "deno",
    "irb", "pry", "iex", "ghci",
    "psql", "mysql", "sqlite3", "mongo", "redis-cli",
    "top", "htop", "btop", "atop",
    "watch", "tmux", "screen",
}

# ── Shell operators that indicate complex pipelines / redirections ────────
_SHELL_OPERATORS = ["&&", "||", " | ", "$(", "`", "<<", ">>"]

# Characters that indicate redirection (bare >/<)
_REDIRECT_RE_PARTS = [" > ", " < ", " 2> ", " &> "]


# ── Public entry point ────────────────────────────────────────────────────

def process_hook(raw_input: str, precompact: bool = False) -> str:
    """
    Process a Claude Code hook invocation.

    `precompact=True` means this is a PreCompact event: mark dedup cache
    stale and return `{}` so Claude Code proceeds normally.
    """
    if precompact:
        store.mark_all_stale()
        return "{}"

    try:
        data = json.loads(raw_input)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[claude-compress] hook: invalid JSON: {e}", file=sys.stderr)
        return "{}"

    # Guard: must be a dict (e.g. json.loads("null") returns None)
    if not isinstance(data, dict):
        return "{}"

    # Guard: tool must be a Bash-like tool
    tool_name = str(data.get("tool_name", "")).lower()
    if tool_name not in _BASH_TOOL_NAMES:
        return "{}"

    # Extract command from tool_input
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "{}"

    command: str = tool_input.get("command", "") or ""
    if not command or not command.strip():
        return "{}"

    # Guard: skip if command should not be wrapped
    skip_reason = _should_skip(command)
    if skip_reason:
        print(f"[claude-compress] skip ({skip_reason}): {command[:80]}", file=sys.stderr)
        return "{}"

    # Rewrite: append pipe to compression
    base = _base_cmd(command)
    rewritten = f"{command} 2>&1 | claude-compress compress --cmd {base}"

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": rewritten},
        }
    }
    return json.dumps(output)


# ── Guard logic (all miss cases) ──────────────────────────────────────────

def _should_skip(command: str) -> Optional[str]:
    """Return a skip-reason string, or None if the command should be wrapped."""
    cmd = command.strip()
    base = _base_cmd(cmd)

    # Already compressed — avoid double-wrapping
    if "claude-compress compress" in cmd:
        return "already-compressed"

    # Self-invocation
    if base in ("claude-compress", "sqz"):
        return "self"

    # Interactive commands
    if base in _INTERACTIVE_CMDS:
        return "interactive"

    # Watch-mode flags
    if "--watch" in cmd or "-w " in cmd:
        return "watch-mode"

    # Shell operators (pipelines, logical operators)
    for op in _SHELL_OPERATORS:
        if op in cmd:
            return f"shell-op:{op.strip()}"

    # Bare redirections
    for op in _REDIRECT_RE_PARTS:
        if op in cmd:
            return f"redirect:{op.strip()}"

    # Semicolon-separated commands (but not inside quotes)
    if _has_bare_semicolon(cmd):
        return "shell-op:;"

    # Background jobs
    if cmd.endswith(" &") or " & " in cmd:
        return "background-job"

    return None


def _has_bare_semicolon(cmd: str) -> bool:
    """Check for semicolons outside of single/double quotes."""
    in_single = False
    in_double = False
    for ch in cmd:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            return True
    return False


def _base_cmd(cmd: str) -> str:
    first = cmd.strip().split()[0] if cmd.strip() else ""
    return first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
