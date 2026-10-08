#!/usr/bin/env python3
"""dev-spec PreToolUse guard: a small supplemental check, not a permission system.

Covers direct shell commands (split on ; && || | and newlines, parsed with shlex)
and Edit/Write/MultiEdit content. It does not understand interpreters, eval,
variable expansion or heredoc bodies. Native permissions and sandboxing remain
authoritative. Any internal error fails open (no output, exit 0) so a broken
guard never wedges a session; tests cover the intended behaviour.

Decisions:
  deny  - catastrophic deletes, launching Claude with permission bypass,
          committing staged content that matches a high-confidence secret.
  ask   - history/worktree-destroying git commands, force pushes to protected
          branches, broad deletes of the current directory, likely secrets in
          file writes or commits, dispatching a worktree subagent while work it
          cannot see is uncommitted or the worktree base is not HEAD.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Iterable

PROTECTED_BRANCHES = {"main", "master", "develop", "trunk", "release", "production"}
ALLOW_MARKER = "dev-spec: allow-secret"

HIGH_CONFIDENCE_SECRETS = [
    ("私钥", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("Anthropic Key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI Key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{40,}")),
    ("Slack Token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe Live Key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}")),
]
GENERIC_SECRET = re.compile(
    r"""(?ix)(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\w*
        \s*[:=]\s*["']([^"'\s]{12,})["']"""
)
PLACEHOLDER = re.compile(r"(?i)(example|placeholder|changeme|dummy|fake|test|xxx|\*\*\*|<|\$\{|\{\{|your[_-])")

CATASTROPHIC_RM_TARGETS = {
    "/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "${HOME}", "${HOME}/",
    "..", "../", "../*", "/Users", "/home", "/System", "/usr", "/etc", "/var", "/opt", "/bin",
}
BROAD_RM_TARGETS = {".", "./", "*", "./*", ".*"}


# ---------- output helpers ----------

def decide(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": f"[dev-spec guard] {reason}",
    }}, ensure_ascii=False))
    sys.exit(0)


# ---------- shell parsing ----------

SEPARATORS = re.compile(r"\|\||&&|;|\||\n")
WRAPPERS = {"sudo", "command", "builtin", "exec", "nohup", "time", "env", "xargs"}


def split_commands(command: str) -> list[list[str]]:
    """Split a shell string into simple commands (best effort, quote-aware)."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace_split = True
        lexer.whitespace = " \t\r"
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        tokens = None
    if tokens is None:
        parts = SEPARATORS.split(command)
        out = []
        for p in parts:
            try:
                out.append(shlex.split(p, comments=True))
            except ValueError:
                out.append(p.split())
        return [c for c in out if c]
    cmds, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|\n"):
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


def strip_wrappers(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
            continue
        if tok in WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            continue
        break
    return argv[i:]


def short_flags(args: Iterable[str]) -> set[str]:
    flags = set()
    for a in args:
        if a.startswith("--"):
            flags.add(a)
        elif a.startswith("-") and len(a) > 1:
            flags.update("-" + c for c in a[1:])
    return flags


# ---------- shell checks ----------

def check_rm(argv: list[str]) -> None:
    args = argv[1:]
    flags = short_flags(a for a in args if a.startswith("-"))
    recursive = bool(flags & {"-r", "-R", "--recursive"})
    if not recursive:
        return
    targets = [a for a in args if not a.startswith("-")]
    for t in targets:
        norm = t.rstrip("/") or "/"
        if t in CATASTROPHIC_RM_TARGETS or norm in CATASTROPHIC_RM_TARGETS:
            decide("deny", f"拒绝递归删除高危目标 `{t}`。请缩小到具体子目录。")
        home = os.path.expanduser("~")
        if os.path.abspath(os.path.expanduser(t)) in {home, "/"}:
            decide("deny", f"拒绝递归删除主目录或根目录 `{t}`。")
    for t in targets:
        if t in BROAD_RM_TARGETS:
            decide("ask", f"`rm -r {t}` 会清空当前目录内容，确认目录归属后再执行。")


def git_subcommand(argv: list[str]) -> tuple[str, list[str]]:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:]
    return "", []


def check_git(argv: list[str], cwd: str) -> None:
    sub, rest = git_subcommand(argv)
    flags = short_flags(a for a in rest if a.startswith("-"))
    positional = [a for a in rest if not a.startswith("-")]

    if sub == "push":
        force = bool(flags & {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror"}) \
            or any(a.startswith("--force-with-lease=") for a in rest) \
            or any(p.startswith("+") for p in positional[1:])
        if "--delete" in flags or "-d" in flags or any(p.startswith(":") for p in positional[1:]):
            decide("ask", "删除远端引用，确认已获授权。")
        if force:
            refs = positional[1:]
            names = {r.lstrip("+").split(":")[-1].removeprefix("refs/heads/") for r in refs}
            if not refs or names & PROTECTED_BRANCHES or "--mirror" in flags:
                decide("ask", "强制推送可能覆盖受保护分支或未指定分支，确认目标与授权。")
        return

    if sub == "reset" and "--hard" in flags:
        decide("ask", "`git reset --hard` 会丢弃未提交改动，确认这些改动属于本任务且可丢弃。")
    if sub == "clean" and flags & {"-f", "--force"}:
        decide("ask", "`git clean -f` 会删除未跟踪文件（可能含他人产物），确认范围。")
    if sub in {"checkout", "restore"} and "." in positional:
        decide("ask", f"`git {sub} .` 会丢弃工作区改动，确认这些改动可丢弃。")
    if sub == "stash" and positional[:1] and positional[0] in {"drop", "clear"}:
        decide("ask", "丢弃 stash 不可恢复，确认后执行。")
    if sub == "branch" and flags & {"-D"}:
        decide("ask", "`git branch -D` 强制删除未合并分支，确认分支归属。")
    if sub == "worktree" and positional[:1] == ["remove"] and flags & {"-f", "--force"}:
        decide("ask", "强制移除 worktree 会丢失其中未提交改动，确认归属。")
    if sub == "commit":
        scan_commit(rest, flags, cwd)


def check_claude(argv: list[str]) -> None:
    if any(a in {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"} for a in argv) \
            or ("--permission-mode" in argv and "bypassPermissions" in argv):
        decide("deny", "不从会话内启动跳过权限检查的 Claude 实例。")


def check_bash(command: str, cwd: str) -> None:
    for raw in split_commands(command):
        argv = strip_wrappers(raw)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        if prog == "rm":
            check_rm(argv)
        elif prog == "git":
            check_git(argv, cwd)
        elif prog == "claude":
            check_claude(argv)


# ---------- secret scanning ----------

def find_secrets(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    high, generic = [], []
    for line in lines:
        if ALLOW_MARKER in line:
            continue
        for label, rx in HIGH_CONFIDENCE_SECRETS:
            if rx.search(line):
                high.append(label)
        m = GENERIC_SECRET.search(line)
        if m and not PLACEHOLDER.search(m.group(1)):
            generic.append(line.strip()[:60])
    return sorted(set(high)), generic


def scan_commit(rest: list[str], flags: set[str], cwd: str) -> None:
    diff_args = ["git", "diff", "--no-color", "-U0"]
    diff_args += ["HEAD"] if flags & {"-a", "--all"} else ["--cached"]
    try:
        out = subprocess.run(diff_args, cwd=cwd or None, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return
    if out.returncode != 0:
        return
    added = (l[1:] for l in out.stdout.splitlines() if l.startswith("+") and not l.startswith("+++"))
    high, generic = find_secrets(added)
    if high:
        decide("deny", f"待提交内容疑似包含密钥：{', '.join(high)}。移出提交并轮换该凭证；"
                       f"确认是假数据时在该行加注释 `{ALLOW_MARKER}`。")
    if generic:
        decide("ask", f"待提交内容疑似包含硬编码凭证（{len(generic)} 处，例：{generic[0]}），确认后再提交。")


def check_file_write(tool_input: dict) -> None:
    path = str(tool_input.get("file_path", ""))
    chunks = []
    for key in ("content", "new_string"):
        v = tool_input.get(key)
        if isinstance(v, str):
            chunks.append(v)
    for edit in tool_input.get("edits", []) or []:
        if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
            chunks.append(edit["new_string"])
    if not chunks:
        return
    high, generic = find_secrets("\n".join(chunks).splitlines())
    if high:
        decide("ask", f"写入 `{os.path.basename(path)}` 的内容疑似包含密钥（{', '.join(high)}），"
                      "确认这是用户提供且应写入该文件的值。")


# ---------- parallel dispatch ----------

def git(cwd: str, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=cwd or None, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.rstrip("\n") if p.returncode == 0 else None  # keep leading spaces: porcelain status columns


def worktree_base_ref(cwd: str, top: str) -> str:
    """Effective worktree.baseRef: local > project > user settings; default 'fresh'."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    for path in (os.path.join(top, ".claude/settings.local.json"),
                 os.path.join(top, ".claude/settings.json"),
                 os.path.join(home, "settings.json")):
        try:
            with open(path) as f:
                ref = (json.load(f).get("worktree") or {}).get("baseRef")
        except (OSError, ValueError, AttributeError):
            continue
        if ref:
            return ref
    return "fresh"


def check_agent_dispatch(tool_input: dict, cwd: str) -> None:
    """A worktree subagent is created from a commit: uncommitted work and a 'fresh' base are invisible to it."""
    if tool_input.get("isolation") != "worktree":
        return
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return
    status = git(top, "status", "--porcelain", "--untracked-files=normal") or ""
    dirty = []
    for l in status.splitlines():
        entry = l[3:]
        if not l or entry.startswith(".claude/worktrees/"):
            continue
        if l.startswith("??") and ".claude/worktrees/".startswith(entry):   # git folded worktrees into `.claude/`
            expanded = git(top, "status", "--porcelain", "--untracked-files=all", "--", entry,
                           ":(exclude).claude/worktrees") or ""
            dirty += [x[3:] for x in expanded.splitlines() if x]
            continue
        dirty.append(entry)
    if dirty:
        decide("ask", f"主工作树有 {len(dirty)} 个未提交改动（例：{dirty[0]}），worktree 子代理看不到它们。"
                      "先把共享契约提交为检查点；确认这些改动与该子代理无关再继续。")
    if worktree_base_ref(cwd, top) != "head":
        head = git(top, "rev-parse", "HEAD")
        remote = git(top, "rev-parse", "--verify", "-q", "origin/HEAD")
        if remote and head and remote != head:
            decide("ask", "worktree.baseRef 不是 \"head\"：子代理将从远端默认分支创建，看不到当前分支的提交（含契约检查点）。"
                          "在项目 .claude/settings.json 设置 \"worktree\": {\"baseRef\": \"head\"}。")


# ---------- entry ----------

def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(event, dict):
        return
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    if tool == "Bash" and isinstance(tool_input.get("command"), str):
        check_bash(tool_input["command"], cwd)
    elif tool in {"Edit", "Write", "MultiEdit"}:
        check_file_write(tool_input)
    elif tool in {"Agent", "Task"}:
        check_agent_dispatch(tool_input, cwd)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # fail open: the guard is supplemental
        sys.exit(0)
