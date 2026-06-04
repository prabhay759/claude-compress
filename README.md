# claude-compress

> Fewer tokens, lower cost, longer context — automatically.

`claude-compress` sits between Claude Code and every Bash command it runs. Before the output reaches Claude, it is compressed: ANSI codes stripped, repeated lines collapsed, JSON minified, noisy build output folded, and duplicate content replaced with a short reference token. The result is the same information in fewer tokens.

---

## How it works

```
Claude Code                  claude-compress              Your shell
    │                              │                          │
    │  PreToolUse hook fires       │                          │
    │ ─────────────────────────>   │                          │
    │                              │  rewrites command        │
    │  git status                  │  ──────────────────────> │
    │  becomes:                    │                          │ runs
    │  git status 2>&1 |           │                          │ git status
    │  claude-compress compress    │   <────────────────────  │
    │                              │  compresses output       │
    │   <─────────────────────────  │                         │
    │  sees compressed output      │                          │
```

1. `claude-compress init` installs a **PreToolUse hook** in Claude Code's settings
2. The hook fires before every Bash tool call and rewrites the command to pipe its output through `claude-compress compress`
3. Compression runs locally — no network calls, zero telemetry
4. Claude sees the compressed output; a dedup cache means repeated reads return a 13-character reference instead of the full content

---

## Installation

**Requires Python 3.9+**

```sh
git clone https://github.com/prabhay759/claude-compress
cd claude-compress
pip install -e .
```

Verify:

```sh
claude-compress --help
```

---

## Quick start

### Project-level (this repo only)

```sh
claude-compress init
```

Writes hooks to `.claude/settings.local.json` in the current directory.

### Global (every project on this machine)

```sh
claude-compress init --global
```

Writes hooks to `~/.claude/settings.json`. Compression runs in all Claude Code sessions without any per-project setup.

That's it. Open Claude Code and every Bash command is now compressed automatically.

---

## Token savings — real examples

### Repeated log lines  ·  **90% reduction**

```
# Raw output (21 lines)
WARNING: deprecated API call
WARNING: deprecated API call
... × 18 more identical lines ...
Build completed: 1 error

# Compressed (3 lines)
WARNING: deprecated API call (×20)
Build completed: 1 error
```

### Cargo build noise  ·  **88% reduction**

```
# Raw (21 lines — 16 Compiling + error)
   Compiling serde v1.0.0 ...
   Compiling serde v1.0.1 ...
   ... 14 more Compiling lines ...
   Compiling myapp v0.1.0 ...
error[E0308]: mismatched types
  --> src/main.rs:42:5
42 |     do_thing(42u32);
   |              ^^^^^ expected &str, found u32

# Compressed (5 lines — error preserved in full)
   Compiling ... (16 crates)
error[E0308]: mismatched types
  --> src/main.rs:42:5
42 |     do_thing(42u32);
   |              ^^^^^ expected &str, found u32
```

### JSON API response  ·  **69% reduction**

```json
// Raw (318 bytes)
{
  "status": "ok",
  "user": { "id": 42, "name": "prabhay", "avatar": null,
            "bio": null, "location": null,
            "preferences": { "theme": "dark", "notifications": null,
                             "language": "en", "timezone": null } },
  "metadata": null, "errors": null, "warnings": null
}

// Compressed (95 bytes — nulls stripped, compact)
{"status":"ok","user":{"id":42,"name":"prabhay","preferences":{"theme":"dark","language":"en"}}}
```

### Dedup — same file read twice  ·  **100% reduction**

```
# First read: compressed normally
...file contents...

# Second read: 13-character reference
§ref:e31ccbf816f6c042§
```

Claude Code can resolve a `§ref:…§` token back to the original content — so information is not lost, just deduplicated.

---

## Viewing your savings

```sh
claude-compress stats
```

```
Last 24h compression stats:
  Compressions : 42
  Tokens in    : 18,340
  Tokens out   : 7,210
  Tokens saved : 11,130
  Reduction    : 60.7%
```

Pass `--hours` to change the look-back window:

```sh
claude-compress stats --hours 1    # last hour
claude-compress stats --hours 168  # last week
```

During active use you also see per-command feedback in the Claude Code status area:

```
[claude-compress] 41/354 tokens (88% reduction) [cargo]
[claude-compress] 6/2413 tokens (100% reduction) [cat]   ← dedup hit
```

---

## What gets compressed

| Content type | Technique | Typical reduction |
|---|---|---|
| Repeated / identical lines | Run-length encoding (RLE) | 80–99% |
| Cargo / Go build output | Compiling-line folding | 70–90% |
| pytest / jest output | Failure-only extraction | 60–85% |
| JSON API responses | Null stripping + minification | 40–70% |
| git diff context | Unchanged-hunk folding | 20–50% |
| ANSI color codes | Regex strip | 5–20% |
| Repeated file reads | SHA-256 dedup cache | 100% |
| Large outputs (> 500 lines) | Head + tail truncation | variable |

---

## What is NOT compressed

Compression is skipped (returns `{}` passthrough) for:

- **Interactive commands** — `vim`, `ssh`, `python`, `psql`, `less`, `watch`, …
- **Shell pipelines** — commands containing `&&`, `||`, `|`, `;`, `>`, `<`, `&`
- **Command substitution** — `$(…)` or backticks
- **Heredocs** — `<< EOF`
- **Background jobs** — trailing `&`
- **Self-invocation** — `claude-compress …` itself

This means complex one-liners and interactive sessions are always passed through unchanged.

---

## Commands

| Command | Description |
|---|---|
| `claude-compress init` | Install hooks in `.claude/settings.local.json` (project) |
| `claude-compress init --global` | Install hooks in `~/.claude/settings.json` (all projects) |
| `claude-compress hook` | Process PreToolUse JSON from stdin *(called by Claude Code)* |
| `claude-compress hook --precompact` | Mark dedup cache stale before compaction |
| `claude-compress compress --cmd NAME` | Compress stdin and print to stdout *(called via pipe)* |
| `claude-compress resume` | Re-activate dedup cache after SessionStart/compact |
| `claude-compress stats [--hours N]` | Show token savings (default: last 24 h) |
| `claude-compress uninstall` | Remove hooks from project settings |
| `claude-compress uninstall --global` | Remove hooks from global settings |

---

## Uninstalling

Remove from a single project:

```sh
claude-compress uninstall
```

Remove globally:

```sh
claude-compress uninstall --global
```

Both commands surgically remove only the `claude-compress` entries and leave all other hooks in your settings untouched.

---

## Environment variables

| Variable | Effect |
|---|---|
| `CLAUDE_COMPRESS_DIR` | Override the cache directory (default: `~/.claude-compress/`) |

---

## Running tests

```sh
pip install pytest
python -m pytest tests/ -v
```

67 tests covering hook passthrough cases, compression pipeline, and installer merge/uninstall logic.

---

## Privacy

All processing is local. No data leaves your machine. The dedup cache (`~/.claude-compress/cache.db`) is a local SQLite database. There is no telemetry, no network calls, and no external dependencies.
