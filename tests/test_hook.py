import json
import pytest
from claude_compress.hook import process_hook


def _hook(tool_name: str, command: str) -> dict | str:
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    return json.loads(process_hook(json.dumps(payload)))


def rewritten_cmd(tool_name: str, command: str) -> str:
    out = _hook(tool_name, command)
    return out["hookSpecificOutput"]["updatedInput"]["command"]


# ── Should rewrite ────────────────────────────────────────────────────────

def test_rewrites_bash_git_status():
    cmd = rewritten_cmd("Bash", "git status")
    assert "git status" in cmd
    assert "claude-compress compress" in cmd
    assert "--cmd git" in cmd


def test_rewrites_bash_cargo_build():
    cmd = rewritten_cmd("Bash", "cargo build")
    assert "cargo build" in cmd
    assert "--cmd cargo" in cmd


def test_rewrites_stderr_redirect():
    cmd = rewritten_cmd("Bash", "ls -la")
    assert "2>&1" in cmd


def test_rewrites_case_insensitive_tool_name():
    cmd = rewritten_cmd("bash", "pwd")
    assert "claude-compress compress" in cmd


def test_rewrites_run_terminal_command():
    cmd = rewritten_cmd("run_terminal_command", "echo hello")
    assert "claude-compress compress" in cmd


def test_rewrites_execute_bash():
    cmd = rewritten_cmd("execute_bash", "cat README.md")
    assert "claude-compress compress" in cmd


# ── Should passthrough (return {}) ────────────────────────────────────────

def test_passthrough_non_bash_tool():
    assert _hook("Read", "git status") == {}


def test_passthrough_write_tool():
    assert _hook("Write", "anything") == {}


def test_passthrough_empty_command():
    payload = {"tool_name": "Bash", "tool_input": {"command": ""}}
    assert json.loads(process_hook(json.dumps(payload))) == {}


def test_passthrough_blank_command():
    payload = {"tool_name": "Bash", "tool_input": {"command": "   "}}
    assert json.loads(process_hook(json.dumps(payload))) == {}


def test_passthrough_already_compressed():
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "git log 2>&1 | claude-compress compress --cmd git"}}
    assert json.loads(process_hook(json.dumps(payload))) == {}


def test_passthrough_self_invocation():
    assert _hook("Bash", "claude-compress stats") == {}


def test_passthrough_interactive_vim():
    assert _hook("Bash", "vim src/main.py") == {}


def test_passthrough_interactive_ssh():
    assert _hook("Bash", "ssh user@host") == {}


def test_passthrough_interactive_python():
    assert _hook("Bash", "python") == {}


def test_passthrough_interactive_psql():
    assert _hook("Bash", "psql -U postgres mydb") == {}


def test_passthrough_shell_operator_and():
    assert _hook("Bash", "git fetch && git pull") == {}


def test_passthrough_shell_operator_or():
    assert _hook("Bash", "test -f foo || echo missing") == {}


def test_passthrough_pipe_operator():
    assert _hook("Bash", "git log | head -20") == {}


def test_passthrough_command_substitution_dollar():
    assert _hook("Bash", "echo $(date)") == {}


def test_passthrough_backtick_substitution():
    assert _hook("Bash", "echo `date`") == {}


def test_passthrough_heredoc():
    assert _hook("Bash", "cat << EOF\nhello\nEOF") == {}


def test_passthrough_semicolon():
    assert _hook("Bash", "cd /tmp; ls") == {}


def test_passthrough_background_job():
    assert _hook("Bash", "sleep 10 &") == {}


def test_passthrough_watch_flag():
    assert _hook("Bash", "ls --watch") == {}


def test_passthrough_missing_tool_input():
    payload = {"tool_name": "Bash"}
    assert json.loads(process_hook(json.dumps(payload))) == {}


def test_passthrough_missing_command_key():
    payload = {"tool_name": "Bash", "tool_input": {}}
    assert json.loads(process_hook(json.dumps(payload))) == {}


def test_passthrough_invalid_json():
    assert json.loads(process_hook("not json at all")) == {}


def test_passthrough_empty_string():
    assert json.loads(process_hook("")) == {}


def test_passthrough_null_json():
    assert json.loads(process_hook("null")) == {}


# ── PreCompact ────────────────────────────────────────────────────────────

def test_precompact_returns_passthrough():
    result = process_hook("{}", precompact=True)
    assert result == "{}"


# ── Output structure ──────────────────────────────────────────────────────

def test_output_has_correct_keys():
    out = _hook("Bash", "npm test")
    assert "hookSpecificOutput" in out
    hso = out["hookSpecificOutput"]
    assert "updatedInput" in hso
    assert "command" in hso["updatedInput"]


def test_output_permission_decision_allow():
    out = _hook("Bash", "cargo test")
    assert out["hookSpecificOutput"].get("permissionDecision") == "allow"


def test_semicolon_inside_quotes_allowed():
    """A semicolon inside a quoted string is not a shell operator."""
    cmd = rewritten_cmd("Bash", "echo 'hello; world'")
    assert "claude-compress compress" in cmd
