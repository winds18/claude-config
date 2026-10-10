#!/usr/bin/env python3
"""dev-spec integration helper: deterministic fan-in for worktree subagents.

  integrate.py preflight --base SHA           before dispatch: main checkout clean, SHA committed and in HEAD,
                                             worktree.baseRef resolves to "head"
  integrate.py status                        parallel state recovered from git (use after compaction/resume)
  integrate.py plan  [--base SHA] [BRANCH...] dry run: scope, overlaps, dirty worktrees, merge order
  integrate.py apply [--base SHA] [--verify CMD] [--allow-overlap] [BRANCH...]
                                             merge --no-ff in the given order, stop on conflict, run CMD
  integrate.py cleanup [BRANCH...]           remove merged, clean dev-spec worktrees and their branches

Without BRANCH, only dev-spec worktrees are considered: those with an ownership declaration
or located under <repo>/.claude/worktrees/. Other worktrees are never touched unless named.
Ownership comes from the declaration the worktree guard recorded (worktree private git dir,
with a durable copy keyed by branch under <git-common-dir>/dev-spec-owners/ that survives
worktree removal). Renames count as both old and new path. Add --json for machine output.
Run from the main checkout of the integration branch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

OWNER_FILE = "dev-spec-owner.json"
OWNERS_DIR = "dev-spec-owners"
WT_DIR = ".claude/worktrees"


def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def out(*args: str, cwd: str | None = None) -> str:
    p = git(*args, cwd=cwd)
    return p.stdout.rstrip("\n") if p.returncode == 0 else ""  # keep leading spaces: porcelain status columns


def ref(branch: str) -> str:
    return f"refs/heads/{branch}"


# Same semantics as hooks/worktree-guard.py (kept in sync by scripts/test_integrate.py).
def glob_to_regex(pattern: str) -> re.Pattern[str]:
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
    rx, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            rx += "(?:.*/)?"; i += 3
        elif p.startswith("**", i):
            rx += ".*"; i += 2
        elif p[i] == "*":
            rx += "[^/]*"; i += 1
        elif p[i] == "?":
            rx += "[^/]"; i += 1
        else:
            rx += re.escape(p[i]); i += 1
    return re.compile(f"^{rx}{suffix}$")


def matches(path: str, globs: list) -> bool:
    return any(glob_to_regex(g).match(path) for g in globs or [] if isinstance(g, str) and g.strip())


def status_paths(cwd: str | None = None, exclude_worktrees: bool = False) -> list[str]:
    """Changed/untracked paths; renames contribute both sides. With exclude_worktrees, the
    .claude/worktrees/ tree is ignored even when git folds it into an untracked `.claude/`."""
    args = ["status", "--porcelain", "--untracked-files=normal"]
    lines = out(*args, cwd=cwd).splitlines()
    paths = []
    for line in lines:
        if not line:
            continue
        entry = line[3:]
        if exclude_worktrees and line.startswith("??") and (WT_DIR + "/").startswith(entry):
            expanded = out("status", "--porcelain", "--untracked-files=all", "--", entry,
                           f":(exclude){WT_DIR}", cwd=cwd).splitlines()
            paths += [l[3:] for l in expanded if l]
            continue
        if exclude_worktrees and entry.startswith(WT_DIR + "/"):
            continue
        paths += entry.split(" -> ")
    return paths


def changed_files(start: str, branch: str) -> list[str]:
    return [f for f in out("diff", "--name-only", "--no-renames", f"{start}..{ref(branch)}").splitlines() if f]


def common_dir() -> str:
    d = out("rev-parse", "--git-common-dir")
    return os.path.abspath(d) if d else ""


def durable_path(branch: str) -> str:
    return os.path.join(common_dir(), OWNERS_DIR, quote(branch, safe="") + ".json")


def durable_owner(branch: str) -> dict | None:
    """Declaration copy kept in the shared git dir. Trusted only if the commit the worktree was at
    when it declared is still in this branch's history (otherwise it is a stale copy of an old,
    deleted branch that happened to have the same name)."""
    try:
        with open(durable_path(branch)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    anchor = data.get("declared_at")
    if not anchor or git("merge-base", "--is-ancestor", anchor, ref(branch)).returncode != 0:
        return None
    return data


def prune_stale_owner_copies() -> list[str]:
    """Drop copies whose branch no longer exists."""
    d = os.path.join(common_dir(), OWNERS_DIR)
    removed = []
    try:
        names = os.listdir(d)
    except OSError:
        return removed
    from urllib.parse import unquote
    for name in names:
        branch = unquote(name[:-5]) if name.endswith(".json") else ""
        if branch and git("rev-parse", "--verify", "-q", ref(branch)).returncode != 0:
            try:
                os.remove(os.path.join(d, name))
                removed.append(branch)
            except OSError:
                pass
    return removed


def worktrees() -> list[dict]:
    """Linked worktrees (main checkout excluded)."""
    raw = out("worktree", "list", "--porcelain")
    items, cur = [], {}
    for line in raw.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    main_top = os.path.realpath(out("rev-parse", "--show-toplevel"))
    managed_root = os.path.join(main_top, WT_DIR) + os.sep
    result = []
    for w in items:
        path = w.get("worktree", "")
        if not path or os.path.realpath(path) == main_top or "bare" in w:
            continue
        exists = os.path.isdir(path)
        branch = str(w.get("branch", "")).removeprefix("refs/heads/") if w.get("branch") else ""
        owner, declared = None, False
        if exists:
            git_dir = out("rev-parse", "--absolute-git-dir", cwd=path)
            if git_dir:
                try:
                    with open(os.path.join(git_dir, OWNER_FILE)) as f:
                        owner = json.load(f)
                    declared = True
                except (OSError, ValueError):
                    pass
        if owner is None and branch:
            owner = durable_owner(branch)       # scope checks only; never makes a worktree "managed"
        result.append({
            "path": path, "branch": branch, "owner": owner, "exists": exists,
            "dirty": status_paths(cwd=path) if exists else [],
            "locked": "locked" in w, "prunable": "prunable" in w,
            "declared": declared,
            "managed": declared or os.path.realpath(path).startswith(os.path.realpath(managed_root)),
            "head": str(w.get("HEAD", "")),
        })
    return result


def ahead(branch: str) -> int:
    return int(out("rev-list", "--count", f"HEAD..{ref(branch)}") or 0)


def is_merged(branch: str) -> bool:
    return git("merge-base", "--is-ancestor", ref(branch), "HEAD").returncode == 0


def branch_info(branch: str, wts: dict[str, dict], base: str | None, owner_check: bool) -> dict:
    if git("rev-parse", "--verify", "-q", ref(branch)).returncode != 0:
        return {"branch": branch, "error": "分支不存在"}
    start = base or out("merge-base", "HEAD", ref(branch))
    files = changed_files(start, branch)
    wt = wts.get(branch, {})
    owner = wt.get("owner") or durable_owner(branch)
    violations = []
    if owner:
        violations = [f for f in files if matches(f, owner.get("forbidden"))
                      or (owner.get("owned") and not matches(f, owner.get("owned")))]
    return {
        "branch": branch, "sha": out("rev-parse", ref(branch)), "ahead": ahead(branch),
        "merged": is_merged(branch), "files": files, "owner": owner, "owner_check": owner_check,
        "worktree": wt.get("path"), "dirty": wt.get("dirty", []), "violations": violations,
    }


def select(branches: list[str], wts: list[dict]) -> list[str]:
    if branches:
        return branches
    return [w["branch"] for w in wts if w["managed"] and w["branch"] and ahead(w["branch"]) > 0]


def analyse(args) -> dict:
    wt_list = worktrees()
    wts = {w["branch"]: w for w in wt_list if w["branch"]}
    owner_check = not getattr(args, "no_owner_check", False)
    infos = [branch_info(b, wts, args.base, owner_check) for b in select(args.branches, wt_list)]
    touched: dict[str, list[str]] = {}
    for i in infos:
        for f in i.get("files", []):
            touched.setdefault(f, []).append(i["branch"])
    overlaps = {f: bs for f, bs in touched.items() if len(bs) > 1}
    blockers, warnings = [], []
    for i in infos:
        if "error" in i:
            blockers.append(f"{i['branch']}: {i['error']}")
            continue
        if i["merged"]:
            warnings.append(f"{i['branch']}: 已合入 HEAD，跳过")
            continue
        if i["dirty"]:
            blockers.append(f"{i['branch']}: worktree 有 {len(i['dirty'])} 个未提交改动，合并会遗漏它们")
        if i["owner"] is None and owner_check:
            blockers.append(f"{i['branch']}: 找不到归属声明，无法核对越界（确认无需核对时加 --no-owner-check）")
        if i["violations"]:
            blockers.append(f"{i['branch']}: 越界改动 {', '.join(i['violations'][:8])}")
    if overlaps and not getattr(args, "allow_overlap", False):
        for f, bs in sorted(overlaps.items())[:10]:
            blockers.append(f"重叠改动 {f} ← {', '.join(bs)}（归属被破坏或需串行；确认后可 --allow-overlap）")
    for w in wt_list:
        if w["managed"] and not w["branch"]:
            warnings.append(f"detached worktree {w['path']}（HEAD {w['head'][:10]}）不在任何分支上，不会被集成")
        if w["managed"] and not w["exists"]:
            warnings.append(f"worktree 目录已不存在：{w['path']}（git worktree prune 可清理记录）")
    return {"head": out("rev-parse", "--abbrev-ref", "HEAD"), "head_sha": out("rev-parse", "HEAD"),
            "main_dirty": status_paths(exclude_worktrees=True), "branches": infos, "overlaps": overlaps,
            "blockers": blockers, "warnings": warnings}


def run_verify(cmd: str) -> dict:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    tail = (p.stdout + p.stderr).strip().splitlines()[-40:]
    return {"command": cmd, "exit_code": p.returncode, "tail": tail}


def effective_base_ref(top: str) -> tuple[str, str]:
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    for path in (os.path.join(top, ".claude/settings.local.json"), os.path.join(top, ".claude/settings.json"),
                 os.path.join(home, "settings.json")):
        try:
            with open(path) as f:
                value = (json.load(f).get("worktree") or {}).get("baseRef")
        except (OSError, ValueError, AttributeError):
            continue
        if value:
            return value, path
    return "fresh", "默认值"


# ---------- commands ----------

def cmd_preflight(args) -> dict:
    top = out("rev-parse", "--show-toplevel")
    blockers = []
    dirty = status_paths(exclude_worktrees=True)
    if dirty:
        blockers.append(f"主工作树有 {len(dirty)} 个未提交改动（例：{dirty[0]}）：worktree 子代理看不到，先提交契约检查点")
    head = out("rev-parse", "--abbrev-ref", "HEAD")
    if head == "HEAD":
        blockers.append("当前处于 detached HEAD：先切到任务分支再提交检查点")
    # explicit config only: git can synthesise user@hostname, which works but leaks the machine name into history
    if not (out("config", "user.name") and out("config", "user.email")):
        blockers.append("未显式设置 git 身份（user.name / user.email）：提交会失败，或带上自动生成的 用户名@主机名。"
                        "先 git config user.name / user.email（服务器上常见）")
    base = args.base
    if git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        blockers.append(f"基线 {base} 不是本仓库中的提交")
    elif git("merge-base", "--is-ancestor", base, "HEAD").returncode != 0:
        blockers.append(f"基线 {base} 不在当前 HEAD 的历史中")
    ref_value, source = effective_base_ref(top)
    if ref_value != "head":
        # "fresh" starts worktrees from origin/HEAD (local HEAD only when no remote default is known)
        remote = out("rev-parse", "--verify", "-q", "origin/HEAD")
        if remote:
            if git("merge-base", "--is-ancestor", base, "origin/HEAD").returncode != 0:
                blockers.append(f"worktree.baseRef 为 {ref_value!r}（来自 {source}）：子代理从 origin/HEAD 创建，看不到检查点；设为 \"head\"")
        elif out("remote"):
            blockers.append(f"worktree.baseRef 为 {ref_value!r}（来自 {source}）且 origin/HEAD 未知：无法确定 worktree 起点；设为 \"head\"")
    return {"head": head, "base": base, "base_ref": ref_value, "base_ref_source": source,
            "blockers": blockers, "ready": not blockers}


def cmd_status(args) -> dict:
    prune_stale_owner_copies()
    rows = []
    for w in worktrees():
        b = w["branch"]
        rows.append({"branch": b, "worktree": w["path"], "managed": w["managed"], "exists": w["exists"],
                     "locked": w["locked"], "dirty": len(w["dirty"]), "owner": w["owner"],
                     "ahead": ahead(b) if b else None, "merged": is_merged(b) if b else None})
    return {"head": out("rev-parse", "--abbrev-ref", "HEAD"), "head_sha": out("rev-parse", "HEAD"), "worktrees": rows}


def cmd_plan(args) -> dict:
    report = analyse(args)
    if report["main_dirty"]:
        report["blockers"].insert(0, f"主工作树有 {len(report['main_dirty'])} 个未提交改动（例：{report['main_dirty'][0]}），先提交或处理")
    if report["head"] == "HEAD":
        report["blockers"].insert(0, "当前处于 detached HEAD，先切到集成分支")
    report["ready"] = not report["blockers"]
    report["order"] = [i["branch"] for i in report["branches"] if "error" not in i and not i["merged"]]
    return report


def cmd_apply(args) -> dict:
    report = cmd_plan(args)
    report["applied"] = []
    if not report["ready"]:
        return report
    for b in report["order"]:
        p = git("merge", "--no-ff", "--no-edit", "-m", f"merge: 集成 {b}", ref(b))
        if p.returncode != 0:
            git("merge", "--abort")
            report["conflict"] = {"branch": b, "detail": (p.stdout + p.stderr).strip().splitlines()[-15:]}
            break
        report["applied"].append({"branch": b, "merge_sha": out("rev-parse", "HEAD")})
    if args.verify and "conflict" not in report:
        report["verify"] = run_verify(args.verify)
    return report


def cmd_cleanup(args) -> dict:
    wt_list = worktrees()
    by_branch = {w["branch"]: w for w in wt_list if w["branch"]}
    current = out("rev-parse", "--abbrev-ref", "HEAD")
    prune_stale_owner_copies()
    explicit = bool(args.branches)
    targets = args.branches or [w["branch"] for w in wt_list if w["managed"] and w["branch"]]
    done, skipped = [], []
    for b in targets:
        w = by_branch.get(b)
        if b == current:
            skipped.append({"branch": b, "reason": "是当前集成分支"}); continue
        if git("rev-parse", "--verify", "-q", ref(b)).returncode != 0:
            skipped.append({"branch": b, "reason": "分支不存在"}); continue
        if not is_merged(b):
            skipped.append({"branch": b, "reason": "尚未合入 HEAD"}); continue
        if w and w["dirty"]:
            skipped.append({"branch": b, "reason": "worktree 有未提交改动"}); continue
        if w and w["locked"]:
            skipped.append({"branch": b, "reason": "worktree 被锁定（代理仍在运行？）"}); continue
        if w and not explicit and not w["declared"]:
            skipped.append({"branch": b, "reason": "没有归属声明（可能是用户自己的并行会话），需显式点名才清理"}); continue
        if w and not w["exists"]:
            skipped.append({"branch": b, "reason": f"worktree 目录已不存在：确认后用 git worktree prune 清理记录"}); continue
        if w:
            p = git("worktree", "remove", w["path"])
            if p.returncode != 0:
                skipped.append({"branch": b, "reason": p.stderr.strip()}); continue
        p = git("branch", "-d", b)
        if p.returncode != 0:
            skipped.append({"branch": b, "reason": p.stderr.strip()}); continue
        try:
            os.remove(durable_path(b))
        except OSError:
            pass
        done.append(b)
    return {"removed": done, "skipped": skipped}


# ---------- output ----------

def human(cmd: str, r: dict) -> str:
    lines = []
    if cmd == "preflight":
        lines.append(f"分支 {r['head']}，基线 {r['base'][:10]}，worktree.baseRef={r['base_ref']}（{r['base_ref_source']}）")
        lines += [f"  阻塞：{b}" for b in r["blockers"]]
        lines.append("可以派发" if r["ready"] else "存在阻塞，先处理再派发")
        return "\n".join(lines)
    if cmd == "status":
        lines.append(f"HEAD {r['head']} @ {r['head_sha'][:10]}")
        for w in r["worktrees"]:
            own = w["owner"]["owned"] if w["owner"] else "未声明"
            tag = "dev-spec" if w["managed"] else "其他"
            lines.append(f"  [{tag}] {w['branch'] or '(detached)'}  ahead={w['ahead']} merged={w['merged']} "
                         f"dirty={w['dirty']} locked={w['locked']} exists={w['exists']} owned={own}")
        if not r["worktrees"]:
            lines.append("  没有 linked worktree")
        return "\n".join(lines)
    if cmd == "cleanup":
        lines += [f"  已清理 {b}" for b in r["removed"]]
        lines += [f"  跳过 {s['branch']}：{s['reason']}" for s in r["skipped"]]
        return "\n".join(lines) or "  无可清理项"
    lines.append(f"集成到 {r['head']} @ {r['head_sha'][:10]}")
    for i in r["branches"]:
        if "error" in i:
            lines.append(f"  ✗ {i['branch']}: {i['error']}"); continue
        bad = i["violations"] or i["dirty"] or (i["owner"] is None and i["owner_check"])
        lines.append(f"  {'✗' if bad else '✓'} {i['branch']} {i['sha'][:10]} ahead={i['ahead']} files={len(i['files'])}"
                     f"{' (已合入)' if i['merged'] else ''}")
    lines += [f"  阻塞：{b}" for b in r["blockers"]]
    lines += [f"  提示：{w}" for w in r.get("warnings", [])]
    if cmd == "plan":
        lines.append("可以 apply" if r["ready"] else "存在阻塞，未合并任何分支")
    if cmd == "apply":
        for a in r.get("applied", []):
            lines.append(f"  已合并 {a['branch']} → {a['merge_sha'][:10]}")
        if "conflict" in r:
            lines.append(f"  冲突：{r['conflict']['branch']}，已 merge --abort，后续分支未合并")
            lines += [f"    {l}" for l in r["conflict"]["detail"]]
        if "verify" in r:
            v = r["verify"]
            lines.append(f"  验收 `{v['command']}` exit={v['exit_code']}")
            lines += [f"    {l}" for l in v["tail"][-15:]]
    return "\n".join(lines)


def exit_code(cmd: str, r: dict) -> int:
    if cmd in {"plan", "apply", "preflight"} and r.get("blockers"):
        return 2
    if cmd == "apply" and ("conflict" in r or r.get("verify", {}).get("exit_code", 0) != 0):
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="dev-spec worktree 集成助手")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "status", "plan", "apply", "cleanup"):
        s = sub.add_parser(name)
        s.add_argument("--json", action="store_true")
        if name == "preflight":
            s.add_argument("--base", required=True, help="契约检查点提交")
        if name in ("plan", "apply", "cleanup"):
            s.add_argument("branches", nargs="*")
        if name in ("plan", "apply"):
            s.add_argument("--base", help="比较基线（默认各分支与 HEAD 的 merge-base）")
            s.add_argument("--allow-overlap", action="store_true")
            s.add_argument("--no-owner-check", action="store_true", help="允许没有归属声明的分支（显式确认后使用）")
        if name == "apply":
            s.add_argument("--verify", help="合并后在集成状态上运行的验收命令")
    args = ap.parse_args()
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("不在 git 仓库内", file=sys.stderr)
        return 2
    r = {"preflight": cmd_preflight, "status": cmd_status, "plan": cmd_plan, "apply": cmd_apply, "cleanup": cmd_cleanup}[args.cmd](args)
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else human(args.cmd, r))
    return exit_code(args.cmd, r)


if __name__ == "__main__":
    sys.exit(main())
