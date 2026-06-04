"""
Tests for claude_compress.handoff — Phases 1, 2, and 3.

All git operations are exercised against the real repo at
/home/user/claude-compress (we're already inside a git repo).
Store isolation uses the CLAUDE_COMPRESS_DIR env-var fixture.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import claude_compress.handoff as ho


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Each test gets its own handoff directory."""
    monkeypatch.setenv("CLAUDE_COMPRESS_DIR", str(tmp_path))
    import importlib, claude_compress.store as s
    importlib.reload(s)
    importlib.reload(ho)
    yield tmp_path


# ── Phase 1 — Emit ────────────────────────────────────────────────────────

def test_emit_creates_file():
    path = ho.emit(body="## Where this stands\nWorking on auth.")
    assert path.exists()
    assert path.suffix == ".md"


def test_emit_frontmatter_git_derived():
    path = ho.emit(body="## Where this stands\nTest.")
    text = path.read_text()
    meta, _ = ho._parse_frontmatter(text)
    # branch and commit come from real git — just check they're populated
    assert meta.get("branch"), "branch must be non-empty"
    assert meta.get("commit"), "commit must be non-empty"
    assert len(meta["commit"]) == 7


def test_emit_working_tree_field():
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    assert meta["working_tree"] in ("clean", "dirty")


def test_emit_files_touched_matches_git_status():
    """
    The files_touched set must equal what git status --porcelain reports.
    We don't assert exact paths because the repo state varies; we assert
    the count and types are consistent.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True
    )
    expected_count = len([l for l in result.stdout.splitlines() if len(l) >= 4])
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    assert len(meta["files_touched"]) == expected_count


def test_emit_file_sha256_is_hex():
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    for f in meta["files_touched"]:
        sha = f.get("sha256", "")
        assert sha in ("deleted", "unreadable") or all(c in "0123456789abcdef" for c in sha), \
            f"bad sha256: {sha}"


def test_emit_body_preserved():
    body = "## Where this stands\nIn the middle of auth refactor.\n\n## Decisions made\n- Use Redis."
    path = ho.emit(body=body, task="auth refactor")
    _, stored_body = ho._parse_frontmatter(path.read_text())
    assert "auth refactor" in stored_body
    assert "Use Redis" in stored_body


def test_emit_version_field():
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    assert int(meta["clawc_handoff_version"]) == ho.HANDOFF_VERSION


def test_emit_id_format():
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    hid = meta["id"]
    # Format: YYYY-MM-DD-<slug>-<7-char-hash>
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}-.+-[0-9a-f]{7}(-\d+)?$", hid), f"bad id: {hid}"


def test_emit_no_model_invented_paths(tmp_path):
    """Files listed in files_touched must actually appear in git status."""
    git_paths = set()
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            raw = line[3:].strip()
            if " -> " in raw:
                raw = raw.split(" -> ")[-1].strip()
            git_paths.add(raw.strip('"'))

    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    for f in meta["files_touched"]:
        assert f["path"] in git_paths, \
            f"invented path not in git status: {f['path']}"


def test_emit_source_agent_field():
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    assert meta["source_agent"] == "claude-code"


def test_emit_task_and_status_fields():
    path = ho.emit(body="body", task="my task", status="blocked")
    meta, _ = ho._parse_frontmatter(path.read_text())
    assert meta["task"] == "my task"
    assert meta["status"] == "blocked"


# ── Frontmatter round-trip ────────────────────────────────────────────────

def test_frontmatter_roundtrip():
    path = ho.emit(body="## Section\nSome content.")
    text = path.read_text()
    meta, body = ho._parse_frontmatter(text)
    assert "Section" in body
    assert meta["source_agent"] == "claude-code"


def test_parse_frontmatter_no_fm():
    meta, body = ho._parse_frontmatter("just plain text")
    assert meta == {}
    assert body == "just plain text"


# ── Phase 2 — Verify ──────────────────────────────────────────────────────

def test_verify_green_on_fresh_emit():
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    result = ho.verify(meta["id"])
    assert result["signal"] == "GREEN"
    assert result["delta"] == []


def test_verify_red_on_missing_id():
    result = ho.verify("nonexistent-id-xyz")
    assert result["signal"] == "RED"
    assert result["delta"]


def test_verify_yellow_on_modified_file(tmp_path):
    """
    Emit a handoff that records a real file's sha256, modify the file,
    then verify — should be YELLOW with that file named.
    """
    # Write a temp file and track it in the handoff manually
    test_file = tmp_path / "drift_target.txt"
    test_file.write_text("original content")

    # Patch _git_touched_files to include our temp file
    import claude_compress.handoff as ho2
    original_touched = ho2._git_touched_files

    def fake_touched():
        sha = ho2._file_sha256(test_file)
        return [{"path": str(test_file), "status": "modified", "sha256": sha}]

    ho2._git_touched_files = fake_touched
    try:
        path = ho2.emit(body="body")
    finally:
        ho2._git_touched_files = original_touched

    # Now modify the file
    test_file.write_text("modified content — different now")

    meta, _ = ho2._parse_frontmatter(path.read_text())
    result = ho2.verify(meta["id"])
    assert result["signal"] == "YELLOW"
    assert any(str(test_file) in d or "drift_target.txt" in d for d in result["delta"])


def test_verify_yellow_on_deleted_file(tmp_path):
    test_file = tmp_path / "will_be_deleted.txt"
    test_file.write_text("going away")

    import claude_compress.handoff as ho2
    original = ho2._git_touched_files

    def fake_touched():
        sha = ho2._file_sha256(test_file)
        return [{"path": str(test_file), "status": "added", "sha256": sha}]

    ho2._git_touched_files = fake_touched
    try:
        path = ho2.emit(body="body")
    finally:
        ho2._git_touched_files = original

    test_file.unlink()  # delete it

    meta, _ = ho2._parse_frontmatter(path.read_text())
    result = ho2.verify(meta["id"])
    assert result["signal"] == "YELLOW"
    assert any("deleted" in d for d in result["delta"])


def test_verify_returns_meta():
    path = ho.emit(body="body", task="check meta")
    meta, _ = ho._parse_frontmatter(path.read_text())
    result = ho.verify(meta["id"])
    assert result["meta"].get("task") == "check meta"


def test_verify_latest_shorthand():
    ho.emit(body="first")
    ho.emit(body="second")
    result = ho.verify("latest")
    # Should not error — resolves to most recent
    assert result["signal"] in ("GREEN", "YELLOW", "RED")


# ── Phase 3 — Resume ──────────────────────────────────────────────────────

def test_resume_injects_into_claude_md(tmp_path):
    path = ho.emit(body="## Where this stands\nReady to resume.")
    meta, _ = ho._parse_frontmatter(path.read_text())
    inject = tmp_path / "CLAUDE.md"

    result = ho.resume(meta["id"], inject_path=inject)

    assert inject.exists()
    content = inject.read_text()
    assert "Resumed handoff" in content
    assert "Trust:" in content
    assert "Ready to resume" in content


def test_resume_drift_report_comes_first(tmp_path):
    """Trust signal must appear before the narrative body."""
    import claude_compress.handoff as ho2
    original = ho2._git_touched_files

    test_file = tmp_path / "f.txt"
    test_file.write_text("v1")

    def fake_touched():
        return [{"path": str(test_file), "status": "modified",
                 "sha256": ho2._file_sha256(test_file)}]

    ho2._git_touched_files = fake_touched
    try:
        path = ho2.emit(body="## Where this stands\nNarrative here.")
    finally:
        ho2._git_touched_files = original

    # Cause drift
    test_file.write_text("v2 — changed")
    meta, _ = ho2._parse_frontmatter(path.read_text())
    inject = tmp_path / "CLAUDE.md"
    ho2.resume(meta["id"], inject_path=inject)

    content = inject.read_text()
    trust_pos = content.find("Trust:")
    narrative_pos = content.find("Narrative here")
    assert trust_pos < narrative_pos, "drift report must precede narrative"


def test_resume_idempotent(tmp_path):
    """Calling resume twice should not duplicate the injection."""
    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    inject = tmp_path / "CLAUDE.md"

    ho.resume(meta["id"], inject_path=inject)
    ho.resume(meta["id"], inject_path=inject)

    content = inject.read_text()
    assert content.count(ho._INJECT_START) == 1


def test_resume_preserves_existing_content(tmp_path):
    inject = tmp_path / "CLAUDE.md"
    inject.write_text("# Existing content\nDo not lose me.\n")

    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    ho.resume(meta["id"], inject_path=inject)

    content = inject.read_text()
    assert "Do not lose me" in content


def test_clear_resume_removes_injection(tmp_path):
    inject = tmp_path / "CLAUDE.md"
    inject.write_text("# Keep me\n")

    path = ho.emit(body="body")
    meta, _ = ho._parse_frontmatter(path.read_text())
    ho.resume(meta["id"], inject_path=inject)
    assert ho._INJECT_START in inject.read_text()

    removed = ho.clear_resume(inject_path=inject)
    assert removed is True
    content = inject.read_text()
    assert ho._INJECT_START not in content
    assert "Keep me" in content


def test_clear_resume_nonexistent_file(tmp_path):
    removed = ho.clear_resume(inject_path=tmp_path / "no_file.md")
    assert removed is False


# ── list_handoffs ──────────────────────────────────────────────────────────

def test_list_handoffs_empty():
    assert ho.list_handoffs() == []


def test_list_handoffs_newest_first():
    ho.emit(body="first")
    ho.emit(body="second")
    files = ho.list_handoffs()
    assert len(files) == 2
    # Newest is first (sorted by mtime descending)
    assert files[0].stat().st_mtime >= files[1].stat().st_mtime


# ── auto_emit ────────────────────────────────────────────────────────────

def test_auto_emit_produces_artifact():
    path = ho.auto_emit()
    assert path is not None
    assert path.exists()


def test_auto_emit_body_has_sections():
    path = ho.auto_emit()
    _, body = ho._parse_frontmatter(path.read_text())
    assert "Where this stands" in body
    assert "Next concrete step" in body
    assert "Don't redo" in body
