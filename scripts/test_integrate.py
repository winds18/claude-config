#!/usr/bin/env python3
"""Behaviour tests for skills/parallel-dev/scripts/integrate.py on a real repo with linked worktrees.

Run: python3 scripts/test_integrate.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "skills/parallel-dev/scripts/integrate.py"
GUARD = ROOT / "hooks/worktree-guard.py"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_glob_parity() -> None:
    guard = load(ROOT / "hooks/worktree-guard.py", "guard")
    integ = load(TOOL, "integ")
    pats = ["src/api/**", "src/api", "src/", "**/*.test.ts", "src/*.ts", "package-lock.json", "./docs/**", "a?c.md"]
    paths = ["src/api", "src/api/a.ts", "src/apix/a.ts", "src/a.ts", "src/a/b.ts", "x/y/z.test.ts", "z.test.ts",
             "package-lock.json", "sub/package-lock.json", "docs/a.md", "abc.md", "a/c.md"]
    diff = [(p, f) for p in pats for f in paths
            if bool(guard.glob_to_regex(p).match(f)) != bool(integ.glob_to_regex(p).match(f))]
    check("glob 语义与 worktree-guard 一致", not diff, str(diff[:3]))


def main() -> int:
    test_glob_parity()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(os.path.realpath(tmp), "repo")
        repo.mkdir()
        g = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

        def run(*a: str) -> tuple[int, dict]:
            p = subprocess.run([sys.executable, str(TOOL), *a, "--json"], cwd=repo, capture_output=True, text=True)
            try:
                return p.returncode, json.loads(p.stdout)
            except ValueError:
                return p.returncode, {"raw": p.stdout + p.stderr}

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "t")
        (repo / ".gitignore").write_text(".claude/worktrees/\n")
        for d in ("api", "web", "shared"):
            (repo / "src" / d).mkdir(parents=True)
            (repo / "src" / d / "x.py").write_text(f"{d} = 0\n")
        g("add", ".")
        g("commit", "-qm", "contract checkpoint")

        def worktree(name: str, owned: list[str], edits: dict[str, str], declare: bool = True) -> Path:
            wt = repo / ".claude/worktrees" / name
            g("worktree", "add", "-q", "-b", f"wt-{name}", str(wt), "HEAD")
            if declare:   # through the real guard hook, as an implementer would
                ev = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(wt),
                      "tool_input": {"file_path": str(wt / ".dev-spec-owner.json"), "content": json.dumps(
                          {"base": g("rev-parse", "HEAD"), "owned": owned, "forbidden": ["src/shared/**"]})}}
                r = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(ev), capture_output=True, text=True)
                assert "归属已记录" in r.stdout, r.stdout
            for f, body in edits.items():
                (wt / f).parent.mkdir(parents=True, exist_ok=True)
                (wt / f).write_text(body)
            g("add", "-A", cwd=wt)
            g("commit", "-qm", f"feat: {name}", cwd=wt)
            return wt

        checkpoint = g("rev-parse", "HEAD")
        cfg = Path(tmp, "cfg"); cfg.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
        code, pf = run("preflight", "--base", checkpoint)
        check("preflight: 干净且基线在 HEAD 中时可派发", code == 0 and pf["ready"], str(pf))
        (repo / "src/api/contract.py").write_text("X = 1\n")
        code, pf = run("preflight", "--base", checkpoint)
        check("preflight: 未提交的契约被拦", code == 2 and any("未提交" in b for b in pf["blockers"]), str(pf["blockers"]))
        (repo / "src/api/contract.py").unlink()
        (repo / "src/api/x.py").write_text("api = 9\n")      # tracked + modified: porcelain line starts with a space
        code, pf = run("preflight", "--base", checkpoint)
        check("preflight: 报告完整路径（不截断首字符）", any("src/api/x.py" in b for b in pf["blockers"]), str(pf["blockers"]))
        g("checkout", "--", "src/api/x.py")
        code, pf = run("preflight", "--base", "0" * 40)
        check("preflight: 不存在的基线被拦", code == 2, str(pf["blockers"]))
        g("update-ref", "refs/remotes/origin/HEAD", g("rev-list", "--max-parents=0", "HEAD"))
        g("commit", "-q", "--allow-empty", "-m", "ahead of origin")
        code, pf = run("preflight", "--base", g("rev-parse", "HEAD"))
        check("preflight: baseRef 非 head 且领先远端时被拦", code == 2 and any("baseRef" in b for b in pf["blockers"]), str(pf["blockers"]))
        (cfg / "settings.json").write_text(json.dumps({"worktree": {"baseRef": "head"}}))
        code, pf = run("preflight", "--base", g("rev-parse", "HEAD"))
        check("preflight: 用户设置 baseRef=head 时通过", code == 0 and pf["base_ref_source"].endswith("settings.json"), str(pf))
        # baseRef=fresh: 检查点必须在 origin/HEAD 中；有远端但 origin/HEAD 未知时阻塞
        (cfg / "settings.json").write_text("{}")
        g("update-ref", "refs/remotes/origin/HEAD", g("rev-parse", "HEAD"))
        code, pf = run("preflight", "--base", g("rev-parse", "HEAD"))
        check("preflight: 检查点已在 origin/HEAD 中时通过", code == 0, str(pf["blockers"]))
        g("update-ref", "-d", "refs/remotes/origin/HEAD")
        g("remote", "add", "origin", "https://example.invalid/x.git")
        code, pf = run("preflight", "--base", g("rev-parse", "HEAD"))
        check("preflight: 有远端但 origin/HEAD 未知时阻塞", code == 2 and any("无法确定" in b for b in pf["blockers"]), str(pf["blockers"]))
        g("remote", "remove", "origin")
        (cfg / "settings.json").write_text(json.dumps({"worktree": {"baseRef": "head"}}))

        # identity must be configured explicitly: git's synthesised user@hostname leaks the machine name into history
        name_cfg, mail_cfg = g("config", "user.name"), g("config", "user.email")
        g("config", "--unset", "user.name"); g("config", "--unset", "user.email")
        noid = {**os.environ, "HOME": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}
        for k in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            noid.pop(k, None)
        pr = subprocess.run([sys.executable, str(TOOL), "preflight", "--base", g("rev-parse", "HEAD"), "--json"],
                            cwd=repo, capture_output=True, text=True, env=noid)
        check("preflight: 未显式设置 git 身份时阻塞", pr.returncode == 2 and "git 身份" in pr.stdout, pr.stdout[-300:])
        envid = {**noid, "GIT_AUTHOR_NAME": "ci", "GIT_AUTHOR_EMAIL": "ci@example.com",
                 "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@example.com"}
        pr = subprocess.run([sys.executable, str(TOOL), "preflight", "--base", g("rev-parse", "HEAD"), "--json"],
                            cwd=repo, capture_output=True, text=True, env=envid)
        check("preflight: 仅由环境变量提供的身份不被误拦", "git 身份" not in pr.stdout, pr.stdout[-300:])
        g("config", "user.name", name_cfg); g("config", "user.email", mail_cfg)

        wa = worktree("api", ["src/api/**"], {"src/api/x.py": "api = 1\n"})
        wb = worktree("web", ["src/web/**"], {"src/web/x.py": "web = 1\n"})

        code, st = run("status")
        check("status 列出两个 worktree", code == 0 and len(st["worktrees"]) == 2, str(st)[:200])
        check("status 带出归属声明", all(w["owner"] for w in st["worktrees"]))

        code, plan = run("plan")
        check("plan: 干净分支可合并", code == 0 and plan["ready"] and plan["order"] == ["wt-api", "wt-web"], str(plan.get("blockers")))

        # out-of-scope + forbidden edit on a third branch
        wc = worktree("bad", ["src/web/**"], {"src/shared/x.py": "shared = 9\n", "README.md": "x\n"})
        code, plan = run("plan", "wt-bad")
        check("plan: 越界与禁止文件被拦", code == 2 and any("越界" in b for b in plan["blockers"]), str(plan.get("blockers")))

        # overlap
        wd = worktree("dup", ["src/api/**"], {"src/api/x.py": "api = 2\n"})
        code, plan = run("plan", "wt-api", "wt-dup")
        check("plan: 重叠改动被拦", code == 2 and "src/api/x.py" in plan["overlaps"], str(plan.get("overlaps")))

        # undeclared + dirty
        we = worktree("undeclared", [], {"src/web/y.py": "y\n"}, declare=False)
        (we / "src/web/y.py").write_text("dirty\n")
        code, plan = run("plan", "wt-undeclared")
        bl = " ".join(plan.get("blockers", []))
        check("plan: 未声明与未提交改动被拦", code == 2 and "归属声明" in bl and "未提交" in bl, bl)

        # main dirty blocks apply
        (repo / "scratch.txt").write_text("tmp\n")
        code, ap = run("apply", "wt-api", "wt-web")
        check("apply: 主工作树不干净时拒绝", code == 2 and not ap.get("applied"), str(ap.get("blockers"))[:200])
        (repo / "scratch.txt").unlink()

        code, ap = run("apply", "wt-api", "wt-web", "--verify", "grep -q 'api = 1' src/api/x.py && grep -q 'web = 1' src/web/x.py")
        check("apply: 按序合并两个分支", code == 0 and [a["branch"] for a in ap["applied"]] == ["wt-api", "wt-web"], str(ap)[:300])
        check("apply: 在集成状态上运行验收", ap.get("verify", {}).get("exit_code") == 0, str(ap.get("verify")))
        check("apply: 合并提交为 --no-ff", len(g("rev-list", "--merges", "HEAD").splitlines()) == 2)

        code, ap = run("apply", "wt-dup", "--allow-overlap")
        check("apply: 冲突时 abort 并报告", code == 1 and ap.get("conflict", {}).get("branch") == "wt-dup", str(ap)[:300])
        check("apply: 冲突后工作树干净", g("status", "--porcelain") == "")

        code, ap = run("apply", "wt-bad", "--allow-overlap")
        check("apply: 越界分支不会被合并", code == 2 and not ap.get("applied"))

        code, cl = run("cleanup")
        check("cleanup: 只清理已合并的", sorted(cl["removed"]) == ["wt-api", "wt-web"], str(cl))
        check("cleanup: 未合并的保留并说明原因", {s["branch"] for s in cl["skipped"]} >= {"wt-bad", "wt-dup"}, str(cl["skipped"]))
        check("cleanup: worktree 目录已移除", not wa.exists() and not wb.exists())

        code, fail = run("apply", "--verify", "exit 3", "wt-none")
        check("apply: 不存在的分支被拦", code == 2)

        # ---- 回归：复核报告的发现 ----
        # 1. cleanup 不得触碰非 dev-spec 的 worktree（含签出 main 的用户 worktree）
        g("checkout", "-q", "-b", "feat/integration")
        user_wt = Path(tmp, "hotfix")
        g("worktree", "add", "-q", str(user_wt), "main")
        (user_wt / ".env").write_text("SECRET=1\n")
        code, cl = run("cleanup")
        check("回归: cleanup 不碰用户自己的 worktree", user_wt.exists() and g("branch", "--list", "main") != "", str(cl))
        code, plan = run("plan")
        check("回归: plan 默认不把用户 worktree 当成待集成分支", "main" not in [b["branch"] for b in plan["branches"]], str(plan["branches"])[:200])

        # 2. 把 forbidden 文件重命名进 owned 必须被发现
        (repo / "src/shared/s.py").write_text("s\n")
        g("add", "."); g("commit", "-qm", "shared file")
        wt_ren = repo / ".claude/worktrees/ren"
        g("worktree", "add", "-q", "-b", "wt-ren", str(wt_ren), "HEAD")
        ev = {"hook_event_name": "PreToolUse", "tool_name": "Write", "cwd": str(wt_ren),
              "tool_input": {"file_path": str(wt_ren / ".dev-spec-owner.json"),
                             "content": json.dumps({"base": g("rev-parse", "HEAD"), "owned": ["src/api/**"], "forbidden": ["src/shared/**"]})}}
        subprocess.run([sys.executable, str(GUARD)], input=json.dumps(ev), capture_output=True, text=True)
        g("mv", "src/shared/s.py", "src/api/s.py", cwd=wt_ren)
        g("commit", "-qm", "move", cwd=wt_ren)
        code, plan = run("plan", "wt-ren")
        check("回归: 从 forbidden 重命名进 owned 被拦", code == 2 and any("src/shared/s.py" in b for b in plan["blockers"]), str(plan.get("blockers")))

        # 3. worktree 移除/prune 后仍按持久化的归属声明核对
        g("worktree", "remove", "--force", str(wt_ren))
        code, plan = run("plan", "wt-ren")
        check("回归: worktree 移除后仍拦越界", code == 2 and any("越界" in b for b in plan["blockers"]), str(plan.get("blockers")))
        g("branch", "-q", "wt-orphan", "wt-ren")
        code, plan = run("plan", "wt-orphan")
        check("回归: 找不到归属声明的分支被拦", code == 2 and any("归属声明" in b for b in plan["blockers"]), str(plan.get("blockers")))
        code, plan = run("plan", "wt-orphan", "--no-owner-check")
        check("回归: --no-owner-check 显式放行声明检查", not any("归属声明" in b for b in plan["blockers"]), str(plan.get("blockers")))

        # 4. 与分支同名的 tag 不得干扰
        wt_t = worktree("tagged", ["src/web/**"], {"src/web/t.py": "t\n"})
        g("tag", "wt-tagged", "HEAD")      # tag on integration HEAD, same name as branch
        code, plan = run("plan", "wt-tagged")
        info = [b for b in plan["branches"] if b["branch"] == "wt-tagged"][0]
        check("回归: 同名 tag 不影响 ahead/merged 判断", info["ahead"] == 1 and not info["merged"], str(info)[:200])

        # 5. 未 gitignore 的 .claude/worktrees 不应让主工作树永远判脏
        (repo / ".gitignore").write_text("")
        g("add", ".gitignore"); g("commit", "-qm", "unignore")
        code, plan = run("plan", "wt-tagged")
        check("回归: 折叠的 .claude/ 不算主工作树改动", not plan["main_dirty"], str(plan["main_dirty"]))
        (repo / ".claude/notes.md").write_text("x")
        code, plan = run("plan", "wt-tagged")
        check("回归: .claude/ 下真正的未跟踪文件仍被发现", plan["main_dirty"] == [".claude/notes.md"], str(plan["main_dirty"]))
        (repo / ".claude/notes.md").unlink()

        # 7. 过期的持久化副本不得被同名新分支继承，也不能让用户 worktree 被当作 dev-spec 管理
        import urllib.parse
        owners = Path(g("rev-parse", "--absolute-git-dir"), "dev-spec-owners"); owners.mkdir(exist_ok=True)
        unrelated = g("commit-tree", "-m", "orphan", g("rev-parse", "HEAD^{tree}"))
        (owners / "release.json").write_text(json.dumps({"base": "", "owned": ["**"], "forbidden": [], "declared_at": unrelated}))
        rel_wt = Path(tmp, "release")
        g("worktree", "add", "-q", "-b", "release", str(rel_wt), "HEAD")
        (rel_wt / ".env").write_text("K=1\n")
        code, st = run("status")
        row = [w for w in st["worktrees"] if w["branch"] == "release"][0]
        check("回归: 过期副本不被采信、不算 dev-spec 管理", row["owner"] is None and not row["managed"], str(row))
        code, cl = run("cleanup")
        check("回归: 过期副本不会导致用户 worktree 被删", rel_wt.exists() and (rel_wt / ".env").exists(), str(cl))
        (rel_wt / "src/web/r.py").write_text("r\n")
        g("add", "src/web/r.py", cwd=rel_wt); g("commit", "-qm", "release work", cwd=rel_wt)
        code, plan = run("plan", "release")
        check("回归: 显式点名时过期副本视为无声明", any("归属声明" in b for b in plan["blockers"]), str(plan.get("blockers")))

        # 8. .claude/worktrees 下没有声明的 worktree：默认 cleanup 跳过，显式点名才清理
        nd = repo / ".claude/worktrees/nodecl"
        g("worktree", "add", "-q", "-b", "wt-nodecl", str(nd), "HEAD")
        code, cl = run("cleanup")
        check("回归: 未声明的托管 worktree 默认不清理", nd.exists() and any(x["branch"] == "wt-nodecl" and "声明" in x["reason"] for x in cl["skipped"]), str(cl))
        code, cl = run("cleanup", "wt-nodecl")
        check("回归: 显式点名后才清理", not nd.exists() and "wt-nodecl" in cl["removed"], str(cl))

        # 9. 目录已不存在的 worktree：跳过并说明，不做全局 prune
        gone = worktree("gone", ["src/web/**"], {"src/web/g.py": "g\n"})
        g("merge", "-q", "--no-ff", "-m", "m", "refs/heads/wt-gone")
        import shutil; shutil.rmtree(gone)
        code, cl = run("cleanup", "wt-gone")
        check("回归: 目录缺失时跳过而非全局 prune", any(x["branch"] == "wt-gone" and "不存在" in x["reason"] for x in cl["skipped"])
              and "prunable" in g("worktree", "list", "--porcelain"), str(cl))

        # 6. detached worktree 给出提示而不是静默忽略
        det = repo / ".claude/worktrees/det"
        g("worktree", "add", "-q", "--detach", str(det), "HEAD")
        code, plan = run("plan")
        check("回归: detached worktree 有提示", any("detached" in w for w in plan["warnings"]), str(plan["warnings"]))

    failed = [r for r in results if not r[1]]
    for name, ok, detail in failed:
        print(f"FAIL {name}: {detail}")
    print(f"integrate: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
