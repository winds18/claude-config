#!/usr/bin/env python3
"""Behaviour tests for the parallel-development hooks, against a real temp repo with a linked worktree.

  policy-guard.py   Agent dispatch with isolation=worktree
  worktree-guard.py implementer edits (PreToolUse) and completion (Stop)

Run: python3 scripts/test_parallel_guards.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
results: list[tuple[str, bool, str]] = []


def hook(script: str, event: dict, env: dict) -> dict | None:
    p = subprocess.run([sys.executable, str(HOOKS / script)], input=json.dumps(event),
                       capture_output=True, text=True, timeout=30, env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def decision(out: dict | None) -> str | None:
    if out is None:
        return None
    if "decision" in out:
        return out["decision"]
    return out["hookSpecificOutput"]["permissionDecision"]


def check(name: str, got: str | None, want: str | None) -> None:
    results.append((name, got == want, f"want {want}, got {got}"))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = os.path.realpath(tmp)
        repo, wt, cfg = Path(tmp, "repo"), Path(tmp, "repo/.claude/worktrees/a"), Path(tmp, "cfg")
        cfg.mkdir()
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
        g = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
        repo.mkdir()
        g(repo, "init", "-q", "-b", "main")
        g(repo, "config", "user.email", "t@example.com")
        g(repo, "config", "user.name", "t")
        (repo / ".gitignore").write_text(".claude/worktrees/\n")
        (repo / "src").mkdir()
        (repo / "src/a.py").write_text("a = 1\n")
        g(repo, "add", ".")
        g(repo, "commit", "-qm", "init")
        base = g(repo, "rev-parse", "HEAD")

        # ---- dispatch (main session) ----
        agent = lambda: {"tool_name": "Agent", "cwd": str(repo),
                         "tool_input": {"subagent_type": "implementer", "prompt": "x", "isolation": "worktree"}}
        check("dispatch: clean, no remote", decision(hook("policy-guard.py", agent(), env)), None)
        no_iso = agent(); no_iso["tool_input"].pop("isolation")
        (repo / "src/contract.py").write_text("X = 1\n")
        check("dispatch: dirty but no isolation", decision(hook("policy-guard.py", no_iso, env)), None)
        check("dispatch: uncommitted contract", decision(hook("policy-guard.py", agent(), env)), "ask")
        g(repo, "add", ".")
        g(repo, "commit", "-qm", "contract")
        checkpoint = g(repo, "rev-parse", "HEAD")
        g(repo, "update-ref", "refs/remotes/origin/HEAD", base)      # remote default behind local HEAD
        check("dispatch: baseRef fresh, HEAD ahead of origin", decision(hook("policy-guard.py", agent(), env)), "ask")
        (cfg / "settings.json").write_text(json.dumps({"worktree": {"baseRef": "head"}}))
        check("dispatch: user baseRef head", decision(hook("policy-guard.py", agent(), env)), None)
        (cfg / "settings.json").write_text("{}")
        (repo / ".claude").mkdir(exist_ok=True)
        (repo / ".claude/settings.json").write_text(json.dumps({"worktree": {"baseRef": "head"}}))
        check("dispatch: project baseRef head (untracked settings counted as dirty)",
              decision(hook("policy-guard.py", agent(), env)), "ask")
        g(repo, "add", ".claude/settings.json")
        g(repo, "commit", "-qm", "settings")
        check("dispatch: project baseRef head, clean", decision(hook("policy-guard.py", agent(), env)), None)

        # ---- implementer in linked worktree ----
        g(repo, "worktree", "add", "-q", "-b", "wt-a", str(wt), "HEAD")
        edit = lambda path: {"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": str(wt),
                             "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"}}
        stop = lambda active=False: {"hook_event_name": "SubagentStop", "cwd": str(wt), "stop_hook_active": active}
        main_edit = {**edit(str(repo / "src/a.py")), "cwd": str(repo)}
        check("main checkout: guard inactive", decision(hook("worktree-guard.py", main_edit, env)), None)
        check("worktree: edit before declaring ownership", decision(hook("worktree-guard.py", edit(str(wt / "src/a.py")), env)), "deny")
        git_dir = g(wt, "rev-parse", "--absolute-git-dir")
        Path(git_dir, "dev-spec-owner.json").write_text(json.dumps(
            {"base": checkpoint, "owned": ["src/**"], "forbidden": ["src/contract.py"]}))
        check("worktree: owned path", decision(hook("worktree-guard.py", edit(str(wt / "src/a.py")), env)), None)
        check("worktree: relative owned path", decision(hook("worktree-guard.py", edit("src/new/b.py"), env)), None)
        check("worktree: forbidden contract", decision(hook("worktree-guard.py", edit(str(wt / "src/contract.py")), env)), "deny")
        check("worktree: outside owned", decision(hook("worktree-guard.py", edit(str(wt / "README.md")), env)), "deny")
        check("worktree: path outside worktree", decision(hook("worktree-guard.py", edit(f"{tmp}/scratch.txt"), env)), None)
        check("worktree: owner file not committed", "dev-spec-owner" in g(wt, "status", "--porcelain"), False)

        check("stop: no commits since base", decision(hook("worktree-guard.py", stop(), env)), "block")
        (wt / "src/a.py").write_text("a = 2\n")
        check("stop: uncommitted change", decision(hook("worktree-guard.py", stop(), env)), "block")
        check("stop: second stop allowed", decision(hook("worktree-guard.py", stop(True), env)), None)
        g(wt, "commit", "-qam", "feat: a")
        check("stop: committed in scope", decision(hook("worktree-guard.py", stop(), env)), None)
        (wt / "README.md").write_text("bash wrote this\n")       # bypassing Edit via shell
        g(wt, "add", "README.md")
        g(wt, "commit", "-qm", "docs: sneak")
        out = hook("worktree-guard.py", stop(), env)
        check("stop: committed outside scope (via Bash)", decision(out), "block")
        check("stop: reason names the file", "README.md" in (out or {}).get("reason", ""), True)
        g(repo, "worktree", "remove", "--force", str(wt))

    failed = [r for r in results if not r[1]]
    for name, ok, msg in results:
        if not ok:
            print(f"FAIL {name}: {msg}")
    print(f"parallel-guards: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
