#!/usr/bin/env python3
"""dev-spec worktree guard, attached to the `implementer` subagent via frontmatter hooks.

Active only inside a linked git worktree (a parallel writer). In the main checkout it
does nothing, because ownership there is shared and enforced by the integrator.

Ownership declaration
    The agent uses the Write tool on `<worktree>/.dev-spec-owner.json` with
    {"base": "<checkpoint sha>", "owned": ["src/api/**"], "forbidden": ["package-lock.json"]}.
    The hook intercepts that write, validates it and stores it in the worktree's private
    git dir (the hook process is not subject to Claude's worktree isolation, which refuses
    agent commands that touch .git/worktrees/). The write itself is denied with a
    "recorded" message, so the file never enters the working tree or a commit.
    Once recorded the declaration is locked: the agent cannot widen its own scope.

PreToolUse (Edit/Write/MultiEdit/NotebookEdit)
    deny until the declaration exists; deny edits to forbidden or non-owned paths
    inside the worktree. Paths outside the worktree are left to Claude's own checks.
Stop (runs as SubagentStop)
    block once if the worktree has uncommitted changes, no commits since the branch
    was created (reflog; declared base as fallback), or changes outside the declared ownership (this also catches
    writes made through Bash). A second stop is allowed so a legitimate "nothing to
    change" or an unresolvable conflict can be reported instead of looping.

Internal errors fail open; this is drift prevention, not a security boundary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

OWNER_FILE = "dev-spec-owner.json"
DECLARE_NAME = ".dev-spec-owner.json"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def git(cwd: str, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Repo-relative glob: `**` crosses directories, `*`/`?` do not; a literal path or `dir/`
    matches itself and everything below it."""
    p = pattern.strip()
    if p.startswith("./"):
        p = p[2:]
    if p.endswith("/"):
        p += "**"
    if not any(c in p for c in "*?"):
        return re.compile("^" + re.escape(p) + "(?:/.*)?$")
    suffix = ""
    if p.endswith("/**"):
        p, suffix = p[:-3], "(?:/.*)?"
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif p.startswith("**", i):
            out += ".*"; i += 2
        elif p[i] == "*":
            out += "[^/]*"; i += 1
        elif p[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(p[i]); i += 1
    return re.compile(f"^{out}{suffix}$")


def matches(rel: str, globs: list[str]) -> bool:
    return any(glob_to_regex(g).match(rel) for g in globs if isinstance(g, str) and g.strip())


class Worktree:
    def __init__(self, cwd: str):
        self.top = git(cwd, "rev-parse", "--show-toplevel")
        git_dir = git(cwd, "rev-parse", "--absolute-git-dir")
        common = git(cwd, "rev-parse", "--git-common-dir")
        if common and not os.path.isabs(common):
            common = os.path.normpath(os.path.join(cwd, common))
        self.linked = bool(self.top and git_dir and common
                           and os.path.realpath(git_dir) != os.path.realpath(common))
        self.owner_path = os.path.join(git_dir, OWNER_FILE) if git_dir else ""

    def owner(self) -> dict | None:
        try:
            with open(self.owner_path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def rel(self, path: str) -> str | None:
        top = os.path.realpath(self.top)
        p = os.path.realpath(path if os.path.isabs(path) else os.path.join(top, path))
        if p == top or not p.startswith(top + os.sep):
            return None
        return os.path.relpath(p, top).replace(os.sep, "/")


DECLARE_HINT = (f"先用 Write 工具写 worktree 根目录的 `{DECLARE_NAME}` 声明归属，内容："
                '{"base": "<基线SHA>", "owned": ["<负责的 glob>"], "forbidden": ["<禁止修改的 glob>"]}')


def deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": f"[dev-spec worktree] {reason}"}},
                     ensure_ascii=False))


def record_declaration(content: object, wt: "Worktree") -> None:
    try:
        data = json.loads(content) if isinstance(content, str) else None
    except ValueError as e:
        return deny(f"归属声明不是合法 JSON：{e}")
    globs_ok = lambda v: isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
    if not isinstance(data, dict) or not globs_ok(data.get("owned")) or not data["owned"] \
            or not globs_ok(data.get("forbidden", [])) or not isinstance(data.get("base", ""), str):
        return deny('归属声明格式错误：需要 {"base": "<SHA>", "owned": [非空 glob 列表], "forbidden": [glob 列表]}')
    data = {"base": data.get("base", ""), "owned": data["owned"], "forbidden": data.get("forbidden", [])}
    current = wt.owner()
    if current is not None and current != data:
        return deny(f"归属已锁定为 owned={current.get('owned')}, forbidden={current.get('forbidden')}，"
                    "不能自行修改；需要扩大范围时停止并回报负责人。")
    with open(wt.owner_path, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    deny(f"✓ 归属已记录（存于 worktree 私有 git 目录，不进入工作区或提交）：owned={data['owned']}, "
         f"forbidden={data['forbidden']}。这是预期结果，无需重试，也不要用其他方式写这个文件；继续下一步。")


def out_of_scope(rel: str, owner: dict) -> str | None:
    if matches(rel, owner.get("forbidden") or []):
        return "属于禁止修改清单"
    owned = owner.get("owned") or []
    if owned and not matches(rel, owned):
        return "不在负责范围内"
    return None


def pre_tool(event: dict, wt: Worktree) -> None:
    if event.get("tool_name") not in EDIT_TOOLS:
        return
    ti = event.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not isinstance(path, str):
        return
    rel = wt.rel(path)
    if rel is None:
        return
    if rel == DECLARE_NAME:
        return record_declaration(ti.get("content"), wt) if event.get("tool_name") == "Write" \
            else deny(f"用 Write 工具写 `{DECLARE_NAME}` 来声明归属。")
    owner = wt.owner()
    reason = None
    if owner is None:
        reason = f"尚未声明写入归属，worktree 内的编辑被拒绝。{DECLARE_HINT}"
    elif (why := out_of_scope(rel, owner)):
        reason = (f"`{rel}` {why}（owned={owner.get('owned')}, forbidden={owner.get('forbidden')}）。"
                  "需要改动时停止该部分并回报给契约负责人，不要绕过。")
    if reason:
        deny(reason)


def branch_start(wt: Worktree) -> str | None:
    """Commit the worktree branch was created at (oldest reflog entry), so commits that
    landed on the parent branch after the declared base are not blamed on this agent."""
    branch = git(wt.top, "symbolic-ref", "-q", "HEAD")
    if not branch:
        return None
    log = git(wt.top, "reflog", "show", "--format=%H", branch)
    return log.splitlines()[-1] if log else None


def on_stop(event: dict, wt: Worktree) -> None:
    if event.get("stop_hook_active"):
        return
    problems = []
    status = git(wt.top, "status", "--porcelain", "--untracked-files=normal") or ""
    dirty = [l[3:].split(" -> ")[-1] for l in status.splitlines() if l]
    if dirty:
        problems.append(f"有 {len(dirty)} 个未提交改动（例：{dirty[0]}）：按 Conventional Commits 提交到当前分支")
    owner = wt.owner()
    if owner is None:
        problems.append(f"未声明写入归属（{DECLARE_NAME}），主会话无法核对越界")
    else:
        start = branch_start(wt) or owner.get("base")
        changed = list(dirty)
        if isinstance(start, str) and start and git(wt.top, "cat-file", "-e", f"{start}^{{commit}}") is not None:
            if git(wt.top, "rev-parse", "HEAD") == git(wt.top, "rev-parse", start):
                problems.append("自基线以来没有任何提交；若确实无需改动，在回报中说明原因")
            changed += (git(wt.top, "diff", "--name-only", f"{start}..HEAD") or "").splitlines()
        elif start:
            problems.append(f"基线 {start} 不存在，无法核对改动范围")
        bad = sorted({r for r in changed if r and out_of_scope(r, owner)})
        if bad:
            problems.append(f"改动超出归属：{', '.join(bad[:8])}{' …' if len(bad) > 8 else ''}；撤回或在回报中说明并交给负责人")
    if problems:
        print(json.dumps({"decision": "block",
                          "reason": "[dev-spec worktree] 结束前需处理：\n- " + "\n- ".join(problems)},
                         ensure_ascii=False))


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(event, dict):
        return
    wt = Worktree(event.get("cwd") or os.getcwd())
    if not wt.linked:
        return
    name = event.get("hook_event_name", "")
    if name == "PreToolUse":
        pre_tool(event, wt)
    elif name in {"Stop", "SubagentStop"}:
        on_stop(event, wt)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # fail open: supplemental check
        sys.exit(0)
