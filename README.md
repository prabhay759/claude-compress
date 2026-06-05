# claude-compress

> Fewer tokens, lower cost, longer context — automatically.

`claude-compress` sits between Claude Code and every Bash command it runs. Before the output reaches Claude, it is compressed: ANSI codes stripped, repeated lines collapsed, JSON minified, noisy build output folded, and duplicate content replaced with a short reference token. The result is the same information in fewer tokens.

---

## Installation

**Requires Python 3.9+**

```sh
pip install claude-compress
```

Then install the hooks into Claude Code:

```sh
# This project only
claude-compress init

# Every project on this machine (recommended)
claude-compress init --global
```

That's it. Open Claude Code and every Bash command is now compressed automatically.

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
    │  python -m claude_compress   │   <────────────────────  │
    │         compress             │  compresses output       │
    │   <─────────────────────────  │                         │
    │  sees compressed output      │                          │
```

1. `claude-compress init` writes a **PreToolUse hook** into Claude Code's settings
2. The hook fires before every Bash call and rewrites the command to pipe its output through the compression engine
3. Compression runs locally — no network calls, zero telemetry
4. Claude sees the compressed output; a dedup cache means repeated file reads return a 13-character reference instead of the full content

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

Claude Code resolves `§ref:…§` tokens back to the original content — information is not lost, just deduplicated.

---

## Viewing your savings

### Per-command (in tool output)

After every compressed command you see a summary line at the bottom of the tool output, visible directly in Claude Code:

```
git status output here...

[claude-compress: 72% reduction · 354→99 tokens · saved 255] [git]
```

### Session total

```sh
claude-compress stats
```

```
claude-compress  —  last 24h
  [████████████░░░░░░░░]  62% reduction
  Tokens saved : 11,130  (18,340 → 7,210)
  Compressions : 42
```

```sh
claude-compress gain          # one-liner for scripts / status bars
claude-compress stats --hours 1     # last hour
claude-compress stats --hours 168   # last week
```

---

## Configuration

### Scope: project vs global

| Command | Where hooks are written | When it applies |
|---|---|---|
| `claude-compress init` | `.claude/settings.local.json` | This repo only |
| `claude-compress init --global` | `~/.claude/settings.json` | Every project on this machine |

Use `--global` if you want compression in all your Claude Code sessions without per-project setup. Both scopes can coexist — project hooks take precedence.

### Cache directory

By default the dedup cache and stats live in `~/.claude-compress/`. Override with an environment variable:

```sh
export CLAUDE_COMPRESS_DIR=/tmp/my-cache   # use a custom location
```

Add it to your shell profile (`~/.bashrc`, `~/.zshrc`, etc.) to make it permanent.

### Windows / Git Bash

The hooks are installed using your Python interpreter's full path (e.g. `C:\Users\...\python.exe`) so they work in Git Bash without permission issues. Run `claude-compress init --global` from a normal terminal and Claude Code will pick it up automatically.

If you install manually, use the full path to `claude-compress.exe` in your `~/.claude/settings.json` hooks — just like the example below:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash", "hooks": [
        { "type": "command",
          "command": "C:\\Users\\you\\AppData\\Roaming\\Python\\Python312\\Scripts\\claude-compress.exe hook" }
      ]}
    ],
    "PreCompact": [
      { "hooks": [
        { "type": "command",
          "command": "C:\\Users\\you\\AppData\\Roaming\\Python\\Python312\\Scripts\\claude-compress.exe hook --precompact" }
      ]}
    ],
    "SessionStart": [
      { "matcher": "compact", "hooks": [
        { "type": "command",
          "command": "C:\\Users\\you\\AppData\\Roaming\\Python\\Python312\\Scripts\\claude-compress.exe resume" }
      ]}
    ],
    "Stop": [
      { "hooks": [
        { "type": "command",
          "command": "C:\\Users\\you\\AppData\\Roaming\\Python\\Python312\\Scripts\\claude-compress.exe gain" }
      ]}
    ]
  }
}
```

### Session-end savings summary (Stop hook)

`claude-compress init` now also installs a **Stop hook** that prints a one-line savings summary at the end of every Claude Code session:

```
claude-compress: saved 11,130 tokens (62% reduction, 42 compressions)
```

No configuration needed — it's included automatically.

### Session handoff

`claude-compress handoff` lets you snapshot the current session state so a new Claude Code session can resume exactly where you left off — with a trust signal showing whether the repo has drifted.

```sh
# Emit a handoff at the end of a session
claude-compress handoff emit --task "auth refactor" --body "## Where this stands
Finished the JWT middleware, blocked on Redis config."

# In the new session — verify and inject into CLAUDE.md
claude-compress handoff resume          # uses latest handoff
claude-compress handoff verify latest   # check drift without injecting

# List all saved handoffs
claude-compress handoff list

# Remove the injected context once you no longer need it
claude-compress handoff clear
```

Handoffs are stored in `~/.claude-compress/handoffs/` as Markdown files with YAML frontmatter containing the git snapshot (branch, commit SHA, per-file hashes). On resume, the tool diffs the recorded state against the live repo and surfaces a **GREEN / YELLOW / RED** trust signal before the narrative.

Handoffs are also emitted automatically before every context compaction (PreCompact hook) — no manual step needed during long sessions.

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

Compression is skipped (passthrough) for:

- **Interactive commands** — `vim`, `ssh`, `python`, `psql`, `less`, `watch`, …
- **Shell pipelines** — commands containing `&&`, `||`, `|`, `;`, `>`, `<`, `&`
- **Command substitution** — `$(…)` or backticks
- **Heredocs** — `<< EOF`
- **Background jobs** — trailing `&`
- **Self-invocation** — `claude-compress …` itself

Complex one-liners and interactive sessions are always passed through unchanged.

---

## Commands

| Command | Description |
|---|---|
| `claude-compress init` | Install hooks in `.claude/settings.local.json` (project) |
| `claude-compress init --global` | Install hooks in `~/.claude/settings.json` (all projects) |
| `claude-compress hook` | Process PreToolUse JSON from stdin *(called by Claude Code)* |
| `claude-compress hook --precompact` | Auto-emit handoff + mark dedup cache stale |
| `claude-compress compress --cmd NAME` | Compress stdin and print to stdout *(called via pipe)* |
| `claude-compress resume` | Re-activate dedup cache after SessionStart/compact |
| `claude-compress gain [--hours N]` | One-line savings summary — shown at end of each session via Stop hook |
| `claude-compress stats [--hours N]` | Full savings breakdown (default: last 24 h) |
| `claude-compress handoff emit` | Snapshot current session state to a handoff file |
| `claude-compress handoff verify [ID]` | Check a handoff for drift against the live repo |
| `claude-compress handoff resume [ID]` | Inject a handoff into `.claude/CLAUDE.md` |
| `claude-compress handoff list` | List saved handoffs (newest first) |
| `claude-compress handoff clear` | Remove the injected handoff from `CLAUDE.md` |
| `claude-compress uninstall` | Remove hooks from project settings |
| `claude-compress uninstall --global` | Remove hooks from global settings |

---

## Uninstalling

```sh
claude-compress uninstall           # this project only
claude-compress uninstall --global  # all projects
```

Both commands surgically remove only the `claude-compress` entries and leave all other hooks in your settings untouched.

---

## Running tests

```sh
pip install pytest
python -m pytest tests/ -v
```

96 tests covering hook passthrough cases, compression pipeline, handoff emit/verify/resume, and installer merge/uninstall logic.

---

## Privacy

All processing is local. No data leaves your machine. The dedup cache (`~/.claude-compress/cache.db`) is a local SQLite database. There is no telemetry, no network calls, and no external dependencies.
