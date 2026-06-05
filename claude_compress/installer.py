"""
Install / uninstall claude-compress hooks into Claude Code settings files.

Project-level : .claude/settings.local.json  (created in cwd)
Global        : ~/.claude/settings.json
"""

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Literal

Scope = Literal["project", "global"]

_SENTINEL = "claude-compress"

# Hook entries we install
_HOOKS_FRAGMENT = {
    "PreToolUse": [
        {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "claude-compress hook"}],
        }
    ],
    "PreCompact": [
        {
            "hooks": [{"type": "command", "command": "claude-compress hook --precompact"}]
        }
    ],
    "SessionStart": [
        {
            "matcher": "compact",
            "hooks": [{"type": "command", "command": "claude-compress resume"}],
        }
    ],
    "Stop": [
        {
            "hooks": [{"type": "command", "command": "claude-compress gain"}]
        }
    ],
}


# ── Public API ────────────────────────────────────────────────────────────

def install(scope: Scope = "project") -> Path:
    """Install hooks and return the path of the settings file modified."""
    target = _settings_path(scope)
    _ensure_parent(target)
    settings = _load_json_safe(target)
    _merge_hooks(settings)
    _write_json_atomic(target, settings)
    return target


def uninstall(scope: Scope = "project") -> Path:
    """Remove claude-compress hook entries; leave other settings intact."""
    target = _settings_path(scope)
    if not target.exists():
        return target
    settings = _load_json_safe(target)
    _remove_hooks(settings)
    _write_json_atomic(target, settings)
    return target


def is_installed(scope: Scope = "project") -> bool:
    target = _settings_path(scope)
    if not target.exists():
        return False
    settings = _load_json_safe(target)
    hooks = settings.get("hooks", {})
    for entries in hooks.values():
        for entry in entries:
            for h in entry.get("hooks", []):
                if _SENTINEL in h.get("command", ""):
                    return True
    return False


# ── Path helpers ──────────────────────────────────────────────────────────

def _settings_path(scope: Scope) -> Path:
    if scope == "global":
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# ── JSON helpers ──────────────────────────────────────────────────────────

def _load_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        # Backup corrupted file, start fresh
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup)
            print(f"[claude-compress] backed up malformed {path} → {backup}", file=sys.stderr)
        except OSError:
            pass
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Fallback: non-atomic write
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ── Merge / remove logic ──────────────────────────────────────────────────

def _merge_hooks(settings: dict) -> None:
    """
    Idempotent merge: remove any existing claude-compress entries first,
    then append our entries.  All other hooks are preserved.
    """
    _remove_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event, entries in _HOOKS_FRAGMENT.items():
        existing = hooks.setdefault(event, [])
        existing.extend(entries)


def _remove_hooks(settings: dict) -> None:
    """
    Surgically remove only our hook entries by matching the sentinel string.
    Preserves all other user-configured hooks in each event array.
    """
    hooks = settings.get("hooks", {})
    for event in list(hooks.keys()):
        new_entries = []
        for entry in hooks[event]:
            filtered_hooks = [
                h for h in entry.get("hooks", [])
                if _SENTINEL not in h.get("command", "")
            ]
            if filtered_hooks:
                entry = {**entry, "hooks": filtered_hooks}
                new_entries.append(entry)
            elif not entry.get("hooks"):
                # Entry had no hooks list (unusual) — keep it
                new_entries.append(entry)
        if new_entries:
            hooks[event] = new_entries
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
