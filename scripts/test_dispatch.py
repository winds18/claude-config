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
unverified: list[str] = []          # sections skipped for a missing optional tool; printed, never silent


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
    (tmp / "hooks/dev-spec").mkdir(parents=True)          # copy-install layout: <home>/hooks/dev-spec/policy-guard.py
    shutil.copy(ROOT / "hooks/policy-guard.py", tmp / "hooks/dev-spec/policy-guard.py")
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

        # ---------- 回归：第三轮复核发现 ----------
        node = shutil.which("node")
        wf_test = ROOT / "scripts/test_workflows.mjs"

        def wf_validate(args: dict) -> dict:
            f = tmp / "wf-args.json"
            f.write_text(json.dumps(args))
            r = subprocess.run([node, str(wf_test), "--validate-args", str(f)], capture_output=True, text=True)
            return json.loads(r.stdout or "{}")

        def plan_file(obj, name="p.json") -> Path:
            f = tmp / name
            f.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))
            return f

        two = {"packages": [{"name": "a", "goal": "g", "owned": ["src/a/**"], "verify": ["t"]},
                            {"name": "b", "goal": "g", "owned": ["src/b/**"], "verify": ["t"]}]}

        # 1. 提交检查点前做密钥扫描；失败时恢复原暂存区
        g("switch", "-q", "task")
        (repo / "staged.txt").write_text("keep staged\n"); g("add", "staged.txt")
        (repo / "leak.env").write_text("AWS=" + "AKIA" + "QWERTYUIOPASDFGH" + "\n")
        before = g("rev-parse", "HEAD")
        p = run("prepare", str(plan_file(two)), "--commit")
        check("回归: 检查点含密钥时拒绝提交", p.returncode == 2 and "密钥" in p.stderr and g("rev-parse", "HEAD") == before, p.stderr)
        st = g("status", "--porcelain")
        check("回归: 拒绝后恢复原暂存区", "A  staged.txt" in st and "?? leak.env" in st, st)
        (repo / "leak.env").unlink()
        hook = repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
        p = run("prepare", str(plan_file(two)), "--commit")
        st = g("status", "--porcelain")
        check("回归: 提交失败（pre-commit）时恢复暂存区", p.returncode == 2 and "已恢复原暂存区" in p.stderr and "A  staged.txt" in st, p.stderr + st)
        hook.unlink()
        shutil.move(str(tmp / "hooks"), str(tmp / "hooks.off"))
        p = run("prepare", str(plan_file(two)), "--commit")
        check("回归: 找不到守卫时拒绝提交（fail closed）", p.returncode == 2 and "skip-secret-scan" in p.stderr and g("rev-parse", "HEAD") == before, p.stderr)
        shutil.move(str(tmp / "hooks.off"), str(tmp / "hooks"))
        p = run("prepare", str(plan_file(two)), "--commit")
        check("回归: 干净内容正常提交检查点", p.returncode == 0 and g("rev-parse", "HEAD") != before and "staged.txt" in p.stderr, p.stderr)

        # 2. case_design 透传；未知顶层字段与错误类型报错
        p = run("prepare", str(plan_file({**two, "case_design": False})))
        args = json.loads(p.stdout or "{}")
        check("回归: case_design 透传到 args", p.returncode == 0 and args.get("case_design") is False, p.stdout + p.stderr)
        p = run("check", str(plan_file({**two, "case_design": "no"})), "--json")
        check("回归: case_design 非布尔报错", p.returncode == 2, p.stdout)
        p = run("check", str(plan_file({**two, "casedesign": False})), "--json")
        check("回归: 未知顶层字段报错", p.returncode == 2 and "未知的顶层字段" in p.stdout, p.stdout)
        p = run("check", str(plan_file({"packages": [{"name": ["x"], "goal": "g", "owned": ["a/**"], "verify": ["t"]}]})), "--json")
        check("回归: name 非字符串时报错而非崩溃", p.returncode == 2 and "Traceback" not in p.stderr, p.stdout + p.stderr)

        # 3. Markdown：代码段内的分隔符、ASCII 逗号、未转义 |、残留反引号、用例设计开关
        hdr = "| 包名 | 目标 | 负责 | 验收 |\n| --- | --- | --- | --- |\n"
        p = run("prepare", str(plan_file(hdr + "| a | g | `src/api/**`, `src/types/**` | `cd web; pnpm test` |\n"
                                         "| b | g | src/types/** | t |\n", "m1.md")))
        check("回归: ASCII 逗号分隔且重叠被发现", p.returncode == 2 and "归属重叠" in p.stderr, p.stderr)
        p = run("prepare", str(plan_file(hdr + "| a | g | `src/api/**` | `cd web; pnpm test`; `pytest -q \\| tail -5` |\n"
                                         "用例设计: 否\n", "m2.md")))
        args = json.loads(p.stdout or "{}")
        verify = args.get("packages", [{}])[0].get("verify")
        check("回归: 代码段内的 ; 不拆分、\\| 保留", verify == ["cd web; pnpm test", "pytest -q | tail -5"], str(verify) + p.stderr)
        check("回归: Markdown 用例设计开关", args.get("case_design") is False, json.dumps(args, ensure_ascii=False))
        p = run("check", str(plan_file(hdr + "| a | g | src/a/** | pytest -q | tail -5 |\n", "m3.md")), "--json")
        check("回归: 未转义 | 导致列数不符时报错", p.returncode == 2 and "列" in p.stdout, p.stdout)
        p = run("check", str(plan_file(hdr + "| a | g | src/a/** | `pytest` -q |\n", "m4.md")), "--json")
        check("回归: 残留反引号报错", p.returncode == 2 and "反引号" in p.stdout, p.stdout)

        # 4. 跨语言契约：prepare 的真实输出与重叠用例交给 workflow 的真实校验
        if node:
            p = run("prepare", str(plan_file({**two, "contract": "src/types.ts", "case_design": False})))
            v = wf_validate(json.loads(p.stdout))
            check("契约: prepare 输出被 workflow 接受且 case_design 生效", v.get("ok") and v.get("case_design_calls") == 0, str(v))
            cases = [("./src/api/**", "src/api/**"), ("src/a?i/**", "src/abi/**"), ("src/api*", "src/apix/**"),
                     ("./src/**", "src/web/**"), ("src/a/**", "src/ab/**"), ("docs/x.md", "docs/x.md/y")]
            mism = []
            for g1, g2 in cases:
                plan = {"packages": [{"name": "a", "goal": "g", "owned": [g1], "verify": ["t"]},
                                     {"name": "b", "goal": "g", "owned": [g2], "verify": ["t"]}]}
                py = run("check", str(plan_file(plan)), "--json").returncode == 2
                js = not wf_validate({**plan, "base": "a" * 40}).get("ok")
                if py != js:
                    mism.append((g1, g2, py, js))
            check("契约: Python 与 workflow 的重叠判定逐例一致", not mism, str(mism))
            v = wf_validate({**two, "base": "b" * 64})
            check("回归: workflow 接受 SHA-256 基线", v.get("ok"), str(v))
        elif os.environ.get("DEV_SPEC_REQUIRE_NODE") == "1":
            check("契约测试需要 node（DEV_SPEC_REQUIRE_NODE=1 时不允许跳过）", False, "node 不可用")
        else:
            # a device without node (Debian server) still runs everything else; CI sets DEV_SPEC_REQUIRE_NODE=1
            unverified.append("跨语言契约（prepare 输出与重叠判定交给 workflow 校验）：未安装 node，未验证")

        # 5. review-args：内容触发 security、路径按词匹配、文件名 stem 判 contract、未选 security 时提示
        b0 = g("rev-parse", "HEAD")
        commit({"src/middleware/guard.ts": "if (!req.headers.authorization) throw new Error()\n"}, "guard")
        r = review(f"{b0}..HEAD")
        check("回归: 新增代码命中鉴权关键词时加 security", "security" in r.get("lenses", []), json.dumps(r, ensure_ascii=False))
        b1 = g("rev-parse", "HEAD")
        commit({"src/hooks/useCounter.ts": "export const useCounter = () => 1\n", "src/components/index.ts": "export {}\n"}, "ui")
        r = review(f"{b1}..HEAD")
        check("回归: React hooks 与 index.ts 不误加 security/performance",
              "security" not in r.get("lenses", []) and "performance" not in r.get("lenses", []) and r.get("notes"),
              json.dumps(r, ensure_ascii=False))
        b2 = g("rev-parse", "HEAD")
        commit({"src/types.ts": "export type X = 1\n"}, "types file")
        r = review(f"{b2}..HEAD")
        check("回归: 文件名 types.ts 判为 contract", "contract" in r.get("lenses", []), json.dumps(r, ensure_ascii=False))

        # ---------- env：交付与隔离条件 ----------
        fakebin = tmp / "fake-tools"
        fakebin.mkdir()
        ss = fakebin / "ss"
        # one line carries bytes that are not valid UTF-8 (process names are not guaranteed to be)
        ss.write_bytes(b"#!/bin/sh\nprintf 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\\n'\n"
                       b"printf 'LISTEN 0 128 [::]:8090 [::]:* \\377\\376\\n'\nprintf 'LISTEN 0 5 *:5432 *:*\\n'\n")
        ss.chmod(0o755)
        bare_home = tmp / "bare-home"
        bare_home.mkdir()
        env_env = {**env, "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}", "HOME": str(bare_home),
                   "GIT_CONFIG_NOSYSTEM": "1", "SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}
        for k in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            env_env.pop(k, None)             # a developer machine may provide an identity through any of these
        r = subprocess.run([sys.executable, str(tool), "env", "--json"], cwd=repo, capture_output=True, text=True, env=env_env)
        e = json.loads(r.stdout or "{}")
        check("env: 解析监听端口且不因非 UTF-8 输出崩溃", r.returncode == 0 and e.get("listening_ports") == [22, 5432, 8090], r.stdout + r.stderr)
        check("env: 识别 SSH 会话与仓库", e.get("ssh_session") is True and e.get("in_repo") is True, r.stdout)
        check("env: 仓库内显式配置的 git 身份被识别", e.get("git_identity") is True, r.stdout)
        norepo = tmp / "no-identity"
        norepo.mkdir()
        subprocess.run(["git", "init", "-q", str(norepo)], check=True, env=env_env)
        r = subprocess.run([sys.executable, str(tool), "env", "--json"], cwd=norepo, capture_output=True, text=True, env=env_env)
        e = json.loads(r.stdout or "{}")
        check("env: 未显式配置身份时报告无（不采信自动生成的 用户名@主机名）", e.get("git_identity") is False
              and any("git 身份" in n for n in e.get("notes", [])), r.stdout)
        # identity supplied only through the environment (common in CI) is explicit and must be accepted
        ci_env = {**env_env, "GIT_AUTHOR_NAME": "ci", "GIT_AUTHOR_EMAIL": "ci@example.com",
                  "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@example.com"}
        r = subprocess.run([sys.executable, str(tool), "env", "--json"], cwd=norepo, capture_output=True, text=True, env=ci_env)
        check("env: 仅由环境变量提供的身份被接受", json.loads(r.stdout or "{}").get("git_identity") is True, r.stdout)
        # prepare --commit must refuse BEFORE creating a checkpoint that would carry user@hostname
        (norepo / "f.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(norepo), "add", "-A"], check=True, env=ci_env)
        subprocess.run(["git", "-C", str(norepo), "commit", "-qm", "init"], check=True, env=ci_env)
        subprocess.run(["git", "-C", str(norepo), "switch", "-q", "-c", "task"], check=True, env=ci_env)
        (norepo / "contract.txt").write_text("c\n")
        head0 = subprocess.run(["git", "-C", str(norepo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        r = subprocess.run([sys.executable, str(tool), "prepare", str(plan_file(two)), "--commit"], cwd=norepo,
                           capture_output=True, text=True, env=env_env)
        head1 = subprocess.run(["git", "-C", str(norepo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        check("prepare: 无显式身份时在提交检查点之前拒绝", r.returncode == 2 and "git 身份" in r.stderr and head0 == head1
              and "已提交检查点" not in r.stderr, r.stderr)
        # a failing `ss` (old iproute2 without -H) must fall through instead of reporting "no ports"
        ss.write_text("#!/bin/sh\nexit 255\n")
        r = subprocess.run([sys.executable, str(tool), "env", "--json"], cwd=repo, capture_output=True, text=True, env=env_env)
        check("env: ss 失败时回退到其他工具而不是报告无端口", json.loads(r.stdout or "{}").get("port_source") != "ss", r.stdout[-300:])

        # ---------- model：按包指定模型 ----------
        mp = {"packages": [{"name": "a", "goal": "g", "owned": ["src/a/**"], "verify": ["t"], "model": "sonnet"},
                           {"name": "b", "goal": "g", "owned": ["src/b/**"], "verify": ["t"], "model": "claude-opus-5-5"}]}
        p = run("prepare", str(plan_file(mp)))
        args = json.loads(p.stdout or "{}")
        check("model: 合法取值透传到 args", [x.get("model") for x in args.get("packages", [])] == ["sonnet", "claude-opus-5-5"], p.stdout + p.stderr)
        bad_model = {"packages": [{"name": "a", "goal": "g", "owned": ["src/a/**"], "verify": ["t"], "model": "gpt-x"}]}
        p = run("check", str(plan_file(bad_model)), "--json")
        check("model: 非法取值报错", p.returncode == 2 and "model" in p.stdout, p.stdout)
        p = run("prepare", str(plan_file("| 包名 | 目标 | 负责 | 验收 | 模型 |\n| --- | --- | --- | --- | --- |\n| a | g | src/a/** | t | haiku |\n", "mm.md")))
        check("model: Markdown 的「模型」列", json.loads(p.stdout or "{}").get("packages", [{}])[0].get("model") == "haiku", p.stdout + p.stderr)

        p = run("review-args", "--range", "nosuchref..HEAD")
        check("无效范围退出 2", p.returncode == 2 and not p.stdout.strip(), p.stdout + p.stderr)

    passed = sum(ok for _, ok, _ in results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       {detail[:600]}"))
    for note in unverified:
        print(f"  SKIP {note}")
    print(f"dispatch: {passed}/{len(results)} passed" + (f"，{len(unverified)} 段未验证" if unverified else ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
