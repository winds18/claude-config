#!/usr/bin/env python3
"""Behaviour tests for skills/dev-spec-dispatch/scripts/dispatch.py on throwaway git repos.

Run: python3 scripts/test_dispatch.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DISPATCH = ROOT / "skills/dev-spec-dispatch"
SRC_INTEGRATE = ROOT / "skills/parallel-dev/scripts/integrate.py"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pkg(name: str, owned: list, **kw) -> dict:
    return {"name": name, "goal": "g", "owned": owned, "verify": ["x"], **kw}


def test_validation() -> None:
    d = load(SRC_DISPATCH / "scripts/dispatch.py", "dispatch")
    # Same cases as scripts/test_workflows.mjs for prefixOf()
    for g1, g2 in [("./src/api/**", "src/api/**"), ("src/a?i/**", "src/abi/**"), ("src/api*", "src/apix/**"),
                   ("./src/**", "src/web/**"), ("./src/**", "src/api/**"), ("src/**", "src/api/**")]:
        r = d.validate({"packages": [pkg("a", [g1]), pkg("b", [g2])]})
        check(f"重叠：{g1} vs {g2}", not r["ok"] and any("归属重叠" in e for e in r["errors"]), str(r["errors"]))
    r = d.validate({"packages": [pkg("a", ["src/a/**"]), pkg("b", ["src/ab/**"])]})
    check("不重叠：src/a/** vs src/ab/**", r["ok"], str(r["errors"]))

    cases = [
        ({"packages": []}, "packages"),
        ({"packages": [pkg("a", ["x/**"]), pkg("a", ["y/**"])]}, "重复"),
        ({"packages": [{"name": "a", "owned": ["x/**"], "verify": ["t"]}]}, "goal"),
        ({"packages": [{"name": "a", "goal": "g", "owned": [], "verify": ["t"]}]}, "owned"),
        ({"packages": [{"name": "a", "goal": "g", "owned": ["x/**"]}]}, "verify"),
        ({"packages": [pkg("a", ["x/**"], effort="huge")]}, "effort"),
    ]
    for plan, needle in cases:
        r = d.validate(plan)
        check(f"拒绝无效计划（{needle}）", not r["ok"] and any(needle in e for e in r["errors"]), str(r["errors"]))
    r = d.validate({"packages": [pkg("a", ["x/**"], effort="low"), pkg("b", ["y/**"], effort="max")]})
    check("合法 effort 通过", r["ok"], str(r["errors"]))
    r = d.validate({"packages": [pkg("api", ["src/types/**"]), pkg("web", ["src/web/**"], forbidden=["src/types/**"])]})
    check("owned 与他包 forbidden 冲突只提示", r["ok"] and r["hints"] and "api" in r["hints"][0], str(r))


MD_PLAN = """# 计划

契约: src/types/order.ts#Order

| 包名 | 目标 | 负责 | 禁止 | 验收 | 准备 | 资源 | effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| api | 实现订单接口 | src/api/**; tests/api/** | src/types/**，package-lock.json | `pnpm test tests/api`；pnpm lint | pnpm install | 端口 4101 | medium |
| web | 订单页面 | src/web/** | - | pnpm test tests/web | | | |
"""


def make_repo(tmp: Path) -> tuple[Path, Path]:
    """Repo plus an isolated copy of the skills tree in the installed layout (<skills>/<name>/scripts)."""
    skills = tmp / "skills"
    shutil.copytree(SRC_DISPATCH, skills / "dev-spec-dispatch", ignore=shutil.ignore_patterns("__pycache__"))
    (skills / "parallel-dev/scripts").mkdir(parents=True)
    shutil.copy(SRC_INTEGRATE, skills / "parallel-dev/scripts/integrate.py")
    repo = tmp / "repo"
    repo.mkdir()
    return repo, skills / "dev-spec-dispatch/scripts/dispatch.py"


def main() -> int:
    test_validation()
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(os.path.realpath(t))
        repo, tool = make_repo(tmp)
        conf = tmp / "claude-home"
        conf.mkdir()
        (conf / "settings.json").write_text(json.dumps({"worktree": {"baseRef": "head"}}))
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(conf)}

        def g(*a: str) -> str:
            return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

        def run(*a: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(tool), *a], cwd=repo, capture_output=True, text=True, env=env)

        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "t")
        (repo / "README.md").write_text("x\n")
        g("add", "-A")
        g("commit", "-q", "-m", "init")

        md = tmp / "plan.md"
        md.write_text(MD_PLAN)
        p = run("check", str(md), "--json")
        check("Markdown 计划通过 check", p.returncode == 0 and json.loads(p.stdout)["ok"], p.stdout + p.stderr)

        bad = tmp / "bad.md"
        bad.write_text(MD_PLAN.replace("| web | 订单页面 | src/web/** | - | pnpm test tests/web |", "| web | 订单页面 | src/web/** | - |  |"))
        p = run("check", str(bad), "--json")
        r = json.loads(p.stdout or "{}")
        check("Markdown 缺验收报错退出 2", p.returncode == 2 and any("web" in e and "verify" in e for e in r.get("errors", [])),
              p.stdout + p.stderr)
        p = run("check", str(bad))
        check("人类可读输出含错误", p.returncode == 2 and "计划无效" in p.stdout, p.stdout)

        # prepare on main with dirty tree: refuse to commit
        (repo / "src/types").mkdir(parents=True)
        (repo / "src/types/order.ts").write_text("export type Order = {id: string}\n")
        head0 = g("rev-parse", "HEAD")
        p = run("prepare", str(md), "--commit")
        check("main 上拒绝提交检查点", p.returncode == 2 and "默认分支" in p.stderr and not p.stdout.strip()
              and g("rev-parse", "HEAD") == head0, p.stdout + p.stderr)

        # task branch, dirty tree without --commit: preflight blocks, no args
        g("switch", "-q", "-c", "task")
        out_file = tmp / "args.json"
        p = run("prepare", str(md), "--out", str(out_file))
        check("preflight 阻塞时退出 2 且不输出 args", p.returncode == 2 and "阻塞" in p.stderr
              and not p.stdout.strip() and not out_file.exists(), p.stdout + p.stderr)

        # task branch with --commit: checkpoint, args to stdout, base == HEAD
        p = run("prepare", str(md), "--commit")
        head = g("rev-parse", "HEAD")
        try:
            args = json.loads(p.stdout)
        except ValueError:
            args = {}
        check("任务分支上提交检查点", p.returncode == 0 and head != head0 and g("log", "-1", "--format=%s") == "chore: 并行派发检查点"
              and g("status", "--porcelain") == "", p.stdout + p.stderr)
        check("args.base 等于 HEAD", args.get("base") == head, str(args.get("base")))
        expected = {
            "base": head, "contract": "src/types/order.ts#Order",
            "packages": [
                {"name": "api", "goal": "实现订单接口", "owned": ["src/api/**", "tests/api/**"],
                 "forbidden": ["src/types/**", "package-lock.json"], "setup": "pnpm install",
                 "verify": ["pnpm test tests/api", "pnpm lint"], "resources": "端口 4101", "effort": "medium"},
                {"name": "web", "goal": "订单页面", "owned": ["src/web/**"], "verify": ["pnpm test tests/web"]},
            ],
        }
        check("Markdown 表格转换为同构 JSON", args == expected, json.dumps(args, ensure_ascii=False))

        # JSON plan, clean tree, --out file, base in plan replaced
        jp = tmp / "plan.json"
        jp.write_text(json.dumps({"base": "deadbeef", "packages": [pkg("a", ["src/a/**"]), pkg("b", ["src/b/**"])]}))
        p = run("prepare", str(jp), "--out", str(out_file))
        written = json.loads(out_file.read_text()) if out_file.exists() else {}
        check("JSON 计划写入 --out，base 为 HEAD", p.returncode == 0 and written.get("base") == head
              and [x["name"] for x in written.get("packages", [])] == ["a", "b"], p.stdout + p.stderr)

        # preflight blocker passthrough: base not in history cannot happen, so use detached HEAD
        g("switch", "-q", "--detach")
        p = run("prepare", str(jp))
        check("detached HEAD 时 preflight 阻塞透传", p.returncode == 2 and "detached" in p.stderr and not p.stdout.strip(),
              p.stdout + p.stderr)
        g("switch", "-q", "task")

        # invalid plan: prepare refuses before touching git
        ov = tmp / "overlap.json"
        ov.write_text(json.dumps({"packages": [pkg("a", ["src/**"]), pkg("b", ["src/api/**"])]}))
        (repo / "dirty.txt").write_text("d\n")
        p = run("prepare", str(ov), "--commit")
        check("无效计划不提交", p.returncode == 2 and "归属重叠" in p.stderr and g("rev-parse", "HEAD") == head, p.stderr)
        (repo / "dirty.txt").unlink()

        # review-args
        base = g("rev-parse", "HEAD")

        def commit(files: dict[str, str], msg: str) -> None:
            for f, body in files.items():
                (repo / f).parent.mkdir(parents=True, exist_ok=True)
                (repo / f).write_text(body)
            g("add", "-A")
            g("commit", "-q", "-m", msg)

        def review(rng: str) -> dict:
            p = run("review-args", "--range", rng)
            return json.loads(p.stdout) if p.returncode == 0 else {"error": p.stderr}

        commit({"src/foo.py": "a = 1\n" * 10, "tests/test_foo.py": "b = 2\n" * 10}, "small")
        r = review(base)
        check("小改动只 correctness+tests、effort medium", r.get("lenses") == ["correctness", "tests"]
              and r.get("finder_effort") == "medium" and r.get("range") == f"{base}..HEAD", json.dumps(r, ensure_ascii=False))
        check("每个视角给出理由", set(r.get("reasons", {})) == set(r.get("lenses", [])), str(r.get("reasons")))

        s1 = g("rev-parse", "HEAD")
        commit({"src/auth/handler.py": "x = 1\n"}, "auth")
        r = review(f"{s1}..HEAD")
        check("auth 路径加 security", "security" in r.get("lenses", []) and "performance" not in r.get("lenses", []),
              json.dumps(r, ensure_ascii=False))

        s2 = g("rev-parse", "HEAD")
        commit({"src/big.py": "y = 1\n" * 450}, "big")
        r = review(f"{s2}..HEAD")
        check("大改动 effort high 并加 performance", r.get("finder_effort") == "high" and "performance" in r.get("lenses", []),
              json.dumps(r, ensure_ascii=False))

        s3 = g("rev-parse", "HEAD")
        commit({"src/types/user.ts": "export type User = {}\n"}, "types")
        r = review(f"{s3}..HEAD")
        check("接口目录加 contract", "contract" in r.get("lenses", []) and r.get("finder_effort") == "medium",
              json.dumps(r, ensure_ascii=False))

        s4 = g("rev-parse", "HEAD")
        commit({"a/x.py": "1\n", "b/y.py": "2\n", "c/z.py": "3\n"}, "spread")
        r = review(f"{s4}..HEAD")
        check("≥3 个顶层目录加 contract", "contract" in r.get("lenses", []), json.dumps(r, ensure_ascii=False))

        p = run("review-args", "--range", "nosuchref..HEAD")
        check("无效范围退出 2", p.returncode == 2 and not p.stdout.strip(), p.stdout + p.stderr)

    passed = sum(ok for _, ok, _ in results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       {detail[:600]}"))
    print(f"dispatch: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
