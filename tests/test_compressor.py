import pytest
from claude_compress.compressor import (
    compress,
    _collapse_repeated_lines,
    _collapse_blank_lines,
    _fmt_cargo,
    _fmt_git,
    _try_json_minify,
    _ANSI_RE,
)


# ── ANSI stripping ────────────────────────────────────────────────────────

def test_ansi_stripped():
    colored = "\x1b[32mOn branch main\x1b[0m\nnothing to commit\n"
    result = compress(colored, "git")
    assert "\x1b[" not in result


# ── Empty / trivial input ─────────────────────────────────────────────────

def test_empty_input_passthrough():
    assert compress("") == ""


def test_whitespace_only_passthrough():
    assert compress("   \n  ").strip() == ""


# ── Fallback — never raises ───────────────────────────────────────────────

def test_compress_never_raises(monkeypatch):
    import claude_compress.compressor as c
    def boom(text, cmd=""):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(c, "_compress_inner", boom)
    text = "some output\n"
    assert compress(text, "git") == text


# ── Repeated line collapse ────────────────────────────────────────────────

def test_collapse_repeated_lines_three_or_more():
    text = "same line\nsame line\nsame line\nsame line\n"
    result = _collapse_repeated_lines(text)
    assert "×4" in result
    assert result.count("same line") == 1


def test_collapse_repeated_lines_two_unchanged():
    text = "line\nline\nother\n"
    result = _collapse_repeated_lines(text)
    assert result.count("line") == 2


def test_collapse_repeated_lines_preserves_order():
    text = "a\nb\nb\nb\nc\n"
    result = _collapse_repeated_lines(text)
    assert "a" in result
    assert "b (×3)" in result
    assert "c" in result


# ── Blank line collapse ───────────────────────────────────────────────────

def test_collapse_three_blank_lines():
    text = "a\n\n\n\nb\n"
    result = _collapse_blank_lines(text)
    assert "\n\n\n" not in result


def test_single_blank_line_preserved():
    text = "a\n\nb\n"
    result = _collapse_blank_lines(text)
    assert result == "a\n\nb\n"


# ── JSON minification ─────────────────────────────────────────────────────

def test_json_strip_nulls():
    import json
    text = '{"a": 1, "b": null, "c": "hello"}\n'
    result = _try_json_minify(text)
    parsed = json.loads(result)
    assert "b" not in parsed
    assert parsed["a"] == 1


def test_json_minify_nested_nulls():
    import json
    text = '{"x": {"y": null, "z": 2}}\n'
    result = _try_json_minify(text)
    parsed = json.loads(result)
    assert "y" not in parsed["x"]
    assert parsed["x"]["z"] == 2


def test_non_json_passthrough():
    text = "not json at all\n"
    assert _try_json_minify(text) == text


def test_json_no_savings_passthrough():
    # Short JSON that won't shrink meaningfully
    text = '{"a":1}\n'
    result = _try_json_minify(text)
    # Should not raise and should be valid
    assert result


# ── Cargo formatter ───────────────────────────────────────────────────────

def test_cargo_compiling_folded():
    lines = ["   Compiling foo v0.1\n"] * 20 + ["error[E0308]: type mismatch\n"]
    text = "".join(lines)
    result = _fmt_cargo(text)
    assert "(20 crates)" in result
    assert "type mismatch" in result


def test_cargo_single_compiling_folded():
    text = "   Compiling bar v1.0\nerror: failed\n"
    result = _fmt_cargo(text)
    assert "Compiling" in result
    assert "failed" in result


# ── Git formatter ─────────────────────────────────────────────────────────

def test_git_context_folded():
    lines = (
        "diff --git a/foo b/foo\n"
        "--- a/foo\n"
        "+++ b/foo\n"
        "@@ -1,10 +1,10 @@\n"
        " context1\n"
        " context2\n"
        " context3\n"
        " context4\n"
        " context5\n"
        "+added line\n"
        " context6\n"
    )
    result = _fmt_git(lines)
    assert "added line" in result
    assert "unchanged lines" in result


# ── Truncation ────────────────────────────────────────────────────────────

def test_large_output_truncated():
    # Use varied lines so RLE doesn't collapse them before the line-count check
    big = "".join(f"line-{i}\n" for i in range(600))
    result = compress(big, "ls")
    assert "truncated" in result
    assert len(result) < len(big)


def test_truncation_preserves_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_COMPRESS_DIR", str(tmp_path))
    import importlib, claude_compress.store as s, claude_compress.compressor as c
    importlib.reload(s)
    importlib.reload(c)
    lines = [f"unique-tail-{i}\n" for i in range(600)]
    big = "".join(lines)
    result = c.compress(big, "ls")
    assert "unique-tail-0" in result
    assert "unique-tail-599" in result


# ── Compression is smaller ────────────────────────────────────────────────

def test_compression_not_larger_than_original():
    text = "On branch main\nnothing to commit, working tree clean\n"
    result = compress(text, "git")
    # May be same or smaller, but never larger
    assert len(result) <= len(text) + 5  # small tolerance for newline


def test_repeated_content_dedup_or_compressed(tmp_path, monkeypatch):
    """Second call on identical content should return a ref or compressed form."""
    monkeypatch.setenv("CLAUDE_COMPRESS_DIR", str(tmp_path))
    # reload store so it picks up the new path
    import importlib, claude_compress.store as s
    importlib.reload(s)
    import claude_compress.compressor as c
    importlib.reload(c)

    content = "On branch feature/foo\nnothing to commit\n" * 5
    first = c.compress(content, "git")
    second = c.compress(content, "git")
    assert second.startswith("§ref:") or len(second) <= len(content)
