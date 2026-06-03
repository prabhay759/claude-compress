"""
CLI entry point for claude-compress.

Commands:
  init [--global]        Install hooks into Claude Code settings
  hook [--precompact]    Process PreToolUse/PreCompact JSON from stdin
  compress [--cmd NAME]  Compress stdin, print to stdout
  resume                 Re-activate dedup cache after SessionStart/compact
  stats [--hours N]      Show compression savings
  uninstall [--global]   Remove claude-compress hooks
"""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-compress",
        description="Context compression for Claude Code",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Install hooks into Claude Code settings")
    p_init.add_argument("--global", dest="global_scope", action="store_true",
                        help="Install into ~/.claude/settings.json (system-wide)")
    p_init.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")

    # hook
    p_hook = sub.add_parser("hook", help="Process PreToolUse/PreCompact JSON from stdin")
    p_hook.add_argument("--precompact", action="store_true",
                        help="Mark dedup cache stale (PreCompact event)")

    # compress
    p_compress = sub.add_parser("compress", help="Compress stdin and print to stdout")
    p_compress.add_argument("--cmd", default="",
                            help="Base command name (e.g. git, cargo, pytest)")

    # resume
    sub.add_parser("resume", help="Re-activate dedup cache after context compaction")

    # stats
    p_stats = sub.add_parser("stats", help="Show compression statistics")
    p_stats.add_argument("--hours", type=int, default=24,
                         help="Look-back window in hours (default: 24)")

    # uninstall
    p_uninstall = sub.add_parser("uninstall", help="Remove claude-compress hooks")
    p_uninstall.add_argument("--global", dest="global_scope", action="store_true",
                              help="Remove from ~/.claude/settings.json")

    args = parser.parse_args()

    if args.command == "init":
        _cmd_init(args)
    elif args.command == "hook":
        _cmd_hook(args)
    elif args.command == "compress":
        _cmd_compress(args)
    elif args.command == "resume":
        _cmd_resume()
    elif args.command == "stats":
        _cmd_stats(args)
    elif args.command == "uninstall":
        _cmd_uninstall(args)


# ── Command implementations ───────────────────────────────────────────────

def _cmd_init(args) -> None:
    from . import installer
    scope = "global" if args.global_scope else "project"

    if installer.is_installed(scope):
        print(f"[claude-compress] already installed ({scope})")
        return

    if not getattr(args, "yes", False):
        target = installer._settings_path(scope)
        print(f"This will add claude-compress hooks to:\n  {target}\n")
        try:
            answer = input("Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "y"
        if answer not in ("", "y", "yes"):
            print("Aborted.")
            sys.exit(0)

    path = installer.install(scope)
    print(f"[claude-compress] installed ({scope}) → {path}")
    if scope == "project":
        print("  To enable on all machines: claude-compress init --global")


def _cmd_hook(args) -> None:
    from . import hook
    raw = sys.stdin.read()
    result = hook.process_hook(raw, precompact=args.precompact)
    print(result, end="")


def _cmd_compress(args) -> None:
    from . import compressor, store
    import os

    text = sys.stdin.read()
    cmd_name = args.cmd or ""

    # Track file reads for context-ref annotations
    cmd_full = os.environ.get("SQZ_CMD", cmd_name)
    _maybe_track_file(cmd_full, text)

    result = compressor.compress(text, cmd_name)

    orig_tokens = store._estimate_tokens(text)
    comp_tokens = store._estimate_tokens(result)
    store.log_compression(cmd_name, orig_tokens, comp_tokens)

    if orig_tokens > comp_tokens:
        pct = round((orig_tokens - comp_tokens) / orig_tokens * 100)
        print(f"[claude-compress] {comp_tokens}/{orig_tokens} tokens ({pct}% reduction) [{cmd_name}]",
              file=sys.stderr)

    sys.stdout.write(result)
    if result and not result.endswith("\n"):
        sys.stdout.write("\n")


def _cmd_resume() -> None:
    from . import store
    store.mark_all_fresh()
    print("[claude-compress] session resumed — dedup cache re-activated", file=sys.stderr)


def _cmd_stats(args) -> None:
    from . import store
    s = store.compression_stats(since_hours=args.hours)
    if s["compressions"] == 0:
        print(f"No compressions recorded in the last {args.hours}h.")
        return
    print(f"Last {args.hours}h compression stats:")
    print(f"  Compressions : {s['compressions']}")
    print(f"  Tokens in    : {s['original_tokens']:,}")
    print(f"  Tokens out   : {s['compressed_tokens']:,}")
    print(f"  Tokens saved : {s['tokens_saved']:,}")
    print(f"  Reduction    : {s['reduction_pct']}%")


def _cmd_uninstall(args) -> None:
    from . import installer
    scope = "global" if args.global_scope else "project"
    path = installer.uninstall(scope)
    print(f"[claude-compress] uninstalled ({scope}) from {path}")


# ── Helpers ───────────────────────────────────────────────────────────────

def _maybe_track_file(cmd: str, output: str) -> None:
    from . import store
    from pathlib import Path
    parts = cmd.strip().split()
    base = parts[0].rsplit("/", 1)[-1].lower() if parts else ""
    if base in ("cat", "head", "tail", "less", "bat", "read"):
        if len(parts) > 1:
            path = parts[-1]
            if Path(path).suffix:
                store.add_known_file(path)


if __name__ == "__main__":
    main()
