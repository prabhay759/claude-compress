"""
CLI entry point for claude-compress.

Commands:
  init [--global]        Install hooks into Claude Code settings
  hook [--precompact]    Process PreToolUse/PreCompact JSON from stdin
  compress [--cmd NAME]  Compress stdin, print to stdout
  resume                 Re-activate dedup cache after SessionStart/compact
  stats [--hours N]      Show compression savings
  gain [--hours N]       One-line savings summary
  handoff                Session handoff sub-commands (emit/verify/resume/list/clear)
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

    # gain
    p_gain = sub.add_parser("gain", help="One-line token savings summary (good for scripts)")
    p_gain.add_argument("--hours", type=int, default=24,
                        help="Look-back window in hours (default: 24)")

    # handoff (nested sub-subcommands)
    p_ho = sub.add_parser("handoff", help="Session handoff: emit, verify, resume, list, clear")
    ho_sub = p_ho.add_subparsers(dest="handoff_cmd", required=True)

    ho_emit = ho_sub.add_parser("emit", help="Emit a handoff artifact for the current session")
    ho_emit.add_argument("--body", default="",
                         help="Narrative body (markdown). Reads from stdin if not given.")
    ho_emit.add_argument("--task", default="", help="One-line task description")
    ho_emit.add_argument(
        "--status", default="in_progress",
        choices=["in_progress", "blocked", "ready_for_review"],
        help="Task status",
    )

    ho_verify = ho_sub.add_parser("verify", help="Check a handoff for drift against live repo")
    ho_verify.add_argument("id", nargs="?", default="latest",
                           help="Handoff ID or 'latest' (default)")

    ho_resume = ho_sub.add_parser("resume", help="Inject a handoff into .claude/CLAUDE.md")
    ho_resume.add_argument("id", nargs="?", default="latest",
                           help="Handoff ID or 'latest' (default)")

    ho_sub.add_parser("list", help="List available handoffs")

    ho_clear = ho_sub.add_parser("clear", help="Remove handoff injection from CLAUDE.md")
    ho_clear.add_argument("--inject-path", default=None,
                          help="Path to CLAUDE.md (default: .claude/CLAUDE.md)")

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
    elif args.command == "gain":
        _cmd_gain(args)
    elif args.command == "handoff":
        _cmd_handoff(args)
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
    from . import handoff as ho, hook
    raw = sys.stdin.read()
    if args.precompact:
        # Auto-emit a stub handoff before marking the cache stale.
        path = ho.auto_emit()
        if path:
            print(f"[claude-compress] handoff auto-emitted → {path}", file=sys.stderr)
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
    bar = _savings_bar(s["reduction_pct"])
    print(f"\nclaude-compress  —  last {args.hours}h")
    print(f"  {bar}  {s['reduction_pct']}% reduction")
    print(f"  Tokens saved : {s['tokens_saved']:,}  ({s['original_tokens']:,} → {s['compressed_tokens']:,})")
    print(f"  Compressions : {s['compressions']}")


def _cmd_gain(args) -> None:
    from . import store
    s = store.compression_stats(since_hours=args.hours)
    if s["compressions"] == 0:
        print("claude-compress: no data yet")
        return
    print(
        f"claude-compress: saved {s['tokens_saved']:,} tokens "
        f"({s['reduction_pct']}% reduction, {s['compressions']} compressions)"
    )


def _cmd_handoff(args) -> None:
    from . import handoff as ho
    from pathlib import Path

    cmd = args.handoff_cmd

    if cmd == "emit":
        body = args.body
        if not body:
            # Read from stdin if no --body flag
            if not sys.stdin.isatty():
                body = sys.stdin.read().strip()
        if not body:
            body = ho.BODY_TEMPLATE.format(
                where_this_stands="_Fill in where the session left off._",
                decisions="- _Add key decisions made._",
                blockers="- _Add any blockers._",
                next_step="- _Add the single next concrete step._",
                dont_redo="- _Add dead ends to avoid repeating._",
            )
        path = ho.emit(body=body, task=args.task, status=args.status)
        print(f"Handoff emitted → {path}")
        _print_frontmatter_summary(path)

    elif cmd == "verify":
        result = ho.verify(args.id)
        _print_verify_result(result)

    elif cmd == "resume":
        result = ho.resume(args.id)
        _print_verify_result(result)
        if "injected_to" in result:
            print(f"\nInjected into: {result['injected_to']}")
            print("Open a new Claude Code session — the handoff will be loaded automatically.")

    elif cmd == "list":
        files = ho.list_handoffs()
        if not files:
            print("No handoffs found.")
            return
        print(f"{'ID':<50}  {'Created':<22}  Signal")
        print("-" * 80)
        for f in files:
            meta, _ = ho._parse_frontmatter(f.read_text(encoding="utf-8"))
            hid = meta.get("id", f.stem)
            created = meta.get("created_at", "")[:19].replace("T", " ")
            branch = meta.get("branch", "?")
            print(f"{hid:<50}  {created:<22}  {branch}")

    elif cmd == "clear":
        inject_path = Path(args.inject_path) if args.inject_path else None
        removed = ho.clear_resume(inject_path)
        if removed:
            print("Handoff injection removed from CLAUDE.md.")
        else:
            print("No handoff injection found.")


def _print_verify_result(result: dict) -> None:
    signal = result["signal"]
    delta = result["delta"]
    _ICONS = {"GREEN": "✓", "YELLOW": "⚠", "RED": "✗"}
    icon = _ICONS.get(signal, "?")
    print(f"\n{icon} Trust signal: {signal}")
    if delta:
        print("  Drift:")
        for d in delta:
            print(f"    • {d}")
    else:
        print("  No drift detected — handoff matches live repo state.")


def _print_frontmatter_summary(path) -> None:
    from . import handoff as ho
    meta, _ = ho._parse_frontmatter(path.read_text(encoding="utf-8"))
    print(f"  branch  : {meta.get('branch','?')}")
    print(f"  commit  : {meta.get('commit','?')}")
    print(f"  tree    : {meta.get('working_tree','?')}")
    n = len(meta.get("files_touched", []))
    print(f"  files   : {n} touched")


def _cmd_uninstall(args) -> None:
    from . import installer
    scope = "global" if args.global_scope else "project"
    path = installer.uninstall(scope)
    print(f"[claude-compress] uninstalled ({scope}) from {path}")


# ── Helpers ───────────────────────────────────────────────────────────────

def _savings_bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


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
