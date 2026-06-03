import json
import pytest
from pathlib import Path

from claude_compress import installer


# ── Helpers ───────────────────────────────────────────────────────────────

def make_settings(tmp_path: Path, content: dict, scope: str) -> Path:
    if scope == "project":
        p = tmp_path / ".claude" / "settings.local.json"
    else:
        p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(content), encoding="utf-8")
    return p


def settings_for(tmp_path: Path, scope: str, monkeypatch) -> Path:
    """Patch _settings_path to return a path inside tmp_path."""
    if scope == "project":
        path = tmp_path / ".claude" / "settings.local.json"
    else:
        path = tmp_path / ".claude" / "settings.json"

    monkeypatch.setattr(installer, "_settings_path", lambda _scope: path)
    return path


# ── Install ───────────────────────────────────────────────────────────────

def test_install_creates_file(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    assert path.exists()


def test_install_writes_bash_hook(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    data = json.loads(path.read_text())
    pre_tool = data["hooks"]["PreToolUse"]
    cmds = [h["command"] for e in pre_tool for h in e.get("hooks", [])]
    assert any("claude-compress hook" in c for c in cmds)


def test_install_writes_precompact_hook(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    data = json.loads(path.read_text())
    pre_compact = data["hooks"].get("PreCompact", [])
    cmds = [h["command"] for e in pre_compact for h in e.get("hooks", [])]
    assert any("--precompact" in c for c in cmds)


def test_install_writes_session_start_hook(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    data = json.loads(path.read_text())
    session_start = data["hooks"].get("SessionStart", [])
    cmds = [h["command"] for e in session_start for h in e.get("hooks", [])]
    assert any("resume" in c for c in cmds)


def test_install_idempotent(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    installer.install("project")
    data = json.loads(path.read_text())
    # Should not duplicate hooks
    pre_tool = data["hooks"]["PreToolUse"]
    cmds = [h["command"] for e in pre_tool for h in e.get("hooks", [])]
    bash_hooks = [c for c in cmds if "claude-compress hook" in c and "--precompact" not in c]
    assert len(bash_hooks) == 1


def test_install_preserves_existing_hooks(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "my-linter"}]}
            ]
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing))
    installer.install("project")
    data = json.loads(path.read_text())
    pre_tool = data["hooks"]["PreToolUse"]
    all_cmds = [h["command"] for e in pre_tool for h in e.get("hooks", [])]
    assert "my-linter" in all_cmds
    assert any("claude-compress" in c for c in all_cmds)


def test_install_survives_malformed_json(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json }")
    installer.install("project")
    assert path.exists()
    data = json.loads(path.read_text())
    assert "hooks" in data


def test_install_creates_missing_directory(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    assert not path.parent.exists()
    installer.install("project")
    assert path.parent.exists()


# ── is_installed ──────────────────────────────────────────────────────────

def test_is_installed_false_before_install(tmp_path, monkeypatch):
    settings_for(tmp_path, "project", monkeypatch)
    assert not installer.is_installed("project")


def test_is_installed_true_after_install(tmp_path, monkeypatch):
    settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    assert installer.is_installed("project")


# ── Uninstall ─────────────────────────────────────────────────────────────

def test_uninstall_removes_hooks(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    installer.install("project")
    installer.uninstall("project")
    data = json.loads(path.read_text())
    assert "hooks" not in data or data.get("hooks") == {}


def test_uninstall_preserves_other_hooks(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "my-linter"}]}
            ]
        }
    }
    path.write_text(json.dumps(existing))
    installer.install("project")
    installer.uninstall("project")
    data = json.loads(path.read_text())
    pre_tool = data.get("hooks", {}).get("PreToolUse", [])
    all_cmds = [h["command"] for e in pre_tool for h in e.get("hooks", [])]
    assert "my-linter" in all_cmds
    assert not any("claude-compress" in c for c in all_cmds)


def test_uninstall_nonexistent_file_ok(tmp_path, monkeypatch):
    path = settings_for(tmp_path, "project", monkeypatch)
    assert not path.exists()
    installer.uninstall("project")  # should not raise
