#!/usr/bin/env python3
"""Behaviour tests for hooks/policy-guard.py. Run: python3 scripts/test_policy_guard.py"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "hooks" / "policy-guard.py"
# Built at runtime so this file never contains a literal key.
FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLQ"
FAKE_PASSWORD = "Zq8v" + "Lm2R" + "k9Tx"  # random, contains no placeholder words


def run(event: dict) -> str | None:
    p = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event),
                       capture_output=True, text=True, timeout=20)
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return None
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"]


def bash(cmd: str, cwd: str = "/tmp") -> str | None:
    return run({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd})


CASES = [
    # (command, expected decision or None)
    ("rm -rf /", "deny"),
    ("rm -rf ~", "deny"),
    ("rm -fr $HOME", "deny"),
    ("sudo rm -r -f /usr", "deny"),
    ("cd build && rm -rf ..", "deny"),
    ("rm -rf .", "ask"),
    ("rm -rf ./build", None),
    ("rm -rf node_modules dist", None),
    ("rm file.txt", None),
    ('rm -rf "/tmp/my dir/out"', None),
    ("echo 'rm -rf /'", None),
    ("git status && git diff", None),
    ("git push origin feature/x", None),
    ("git push --force-with-lease origin feature/x", None),
    ("git push -f origin main", "ask"),
    ("git push --force", "ask"),
    ("git push origin +master", "ask"),
    ("git push origin --delete old-branch", "ask"),
    ("git reset --hard HEAD~1", "ask"),
    ("git reset --soft HEAD~1", None),
    ("git clean -fdx", "ask"),
    ("git clean -n", None),
    ("git checkout .", "ask"),
    ("git checkout -b feat/a", None),
    ("git restore --staged src/a.ts", None),
    ("git stash drop", "ask"),
    ("git stash list", None),
    ("git branch -D tmp", "ask"),
    ("git branch -d merged", None),
    ("git -C ../other reset --hard", "ask"),
    ("git worktree remove --force .claude/worktrees/x", "ask"),
    ("claude --dangerously-skip-permissions -p hi", "deny"),
    ("claude -p 'summarize'", None),
]


def test_shell_cases() -> list[str]:
    fails = []
    for cmd, want in CASES:
        got = bash(cmd)
        if got != want:
            fails.append(f"bash {cmd!r}: want {want}, got {got}")
    return fails


def test_file_writes() -> list[str]:
    fails = []
    cases = [
        ({"tool_name": "Write", "tool_input": {"file_path": "a.py", "content": f"TOKEN = '{FAKE_GH}'"}}, "ask"),
        ({"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y = 1"}}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": "README.md", "content": "set api_key: 'your-api-key-here'"}}, None),
        ({"tool_name": "Write", "tool_input": {"file_path": "k.py",
          "content": f"K = '{FAKE_AWS}'  # dev-spec: allow-secret"}}, None),
        ({"tool_name": "Read", "tool_input": {"file_path": "x"}}, None),
    ]
    for event, want in cases:
        got = run(event)
        if got != want:
            fails.append(f"{event['tool_name']} {event['tool_input'].get('file_path')}: want {want}, got {got}")
    return fails


def test_commit_scan() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as d:
        g = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, check=True)
        g("init", "-q")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "t")
        Path(d, "ok.txt").write_text("hello\n")
        g("add", "ok.txt")
        if (got := bash("git commit -m ok", d)) is not None:
            fails.append(f"clean commit: want None, got {got}")
        Path(d, "cfg.py").write_text(f"KEY = '{FAKE_AWS}'\n")
        g("add", "cfg.py")
        if (got := bash("git add . && git commit -m 'add cfg'", d)) != "deny":
            fails.append(f"secret commit: want deny, got {got}")
        g("reset", "-q")
        Path(d, "cfg.py").write_text(f"db_password = '{FAKE_PASSWORD}'\n")
        g("add", "cfg.py")
        if (got := bash("git commit -m generic", d)) != "ask":
            fails.append(f"generic secret commit: want ask, got {got}")
    return fails


def test_garbage_input() -> list[str]:
    p = subprocess.run([sys.executable, str(GUARD)], input="not json", capture_output=True, text=True)
    return [] if p.returncode == 0 and not p.stdout.strip() else ["garbage input should fail open silently"]


def main() -> int:
    fails = test_shell_cases() + test_file_writes() + test_commit_scan() + test_garbage_input()
    total = len(CASES) + 5 + 3 + 1
    for f in fails:
        print("FAIL", f)
    print(f"policy-guard: {total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
