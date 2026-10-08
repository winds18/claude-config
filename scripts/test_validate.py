#!/usr/bin/env python3
"""Tests for scripts/validate.py: cross-reference checks and --changed group selection.

Each case copies the repo into a temp dir, injects one bad reference, and asserts validate.py fails
with an error pointing at the injected file:line. Run: python3 scripts/test_validate.py
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []
IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".tmp", "node_modules", "worktrees")


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def load_validate():
    spec = importlib.util.spec_from_file_location("validate_mod", ROOT / "scripts/validate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def copy_repo(tmp: Path, name: str) -> Path:
    dst = tmp / name
    shutil.copytree(ROOT, dst, ignore=IGNORE, symlinks=True)
    return dst


def run_validate(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(repo / "scripts/validate.py"), *args], cwd=repo,
                          capture_output=True, text=True, errors="replace", timeout=120)


def append(repo: Path, rel: str, text: str) -> int:
    """在文件末尾追加文本，返回追加内容首行的行号。"""
    p = repo / rel
    old = p.read_text()
    if not old.endswith("\n"):
        old += "\n"
    p.write_text(old + text)
    return len(old.splitlines()) + 1


def expect_fail(tmp: Path, name: str, rel: str, text: str, needle: str, offset: int = 0) -> None:
    repo = copy_repo(tmp, re.sub(r"\W+", "_", name))
    line = append(repo, rel, text) + offset
    r = run_validate(repo)
    out = r.stdout + r.stderr
    loc = f"{rel}:{line}"
    hit = any(loc in ln and needle in ln for ln in out.splitlines() if "ERROR" in ln)
    check(f"拦截：{name}", r.returncode != 0 and hit, f"rc={r.returncode} 期望含 {loc} 与 {needle}\n{out[-800:]}")


def test_injections(tmp: Path) -> None:
    expect_fail(tmp, "不存在的 integrate 子命令", "skills/parallel-dev/SKILL.md",
                "\n先运行 `integrate.py merge-all --json`。\n", "merge-all", offset=1)
    expect_fail(tmp, "integrate 子命令不支持的参数", "docs/design.md",
                "\n恢复时 `integrate.py status --base abc123`。\n", "--base", offset=1)
    expect_fail(tmp, "fenced 代码块中的 integrate 参数", "README.md",
                "\n```bash\npython3 integrate.py apply wt-a --verfiy \"pytest --maxfail 1\"\n```\n", "--verfiy", offset=2)
    expect_fail(tmp, "install.sh 不支持的参数", "README.md",
                "\n```bash\nbash install.sh --copy --turbo --apply\n```\n", "--turbo", offset=2)
    expect_fail(tmp, "install.sh update 只接受 --claude-home", "README.md",
                "\n运行 `bash install.sh update --apply` 立即更新。\n", "--apply", offset=1)
    expect_fail(tmp, "未知 workflow 名", "docs/design.md",
                "\n部署用 `/dev-spec-deploy` workflow。\n", "dev-spec-deploy", offset=1)
    expect_fail(tmp, "不存在的 §N", "global/rules/dev-spec/04-git-delivery.md",
                "\n- 细节见 `parallel-dev` §9。\n", "§9", offset=1)
    expect_fail(tmp, "§ 范围越界", "README.md",
                "\n见 `dev-workflow` 技能 §6–8。\n", "§8", offset=1)
    expect_fail(tmp, "未知 agentType（Markdown）", "skills/parallel-dev/SKILL.md",
                "\n并发 `Agent(subagent_type: \"implementor\", isolation: \"worktree\")`。\n", "implementor", offset=1)
    expect_fail(tmp, "未知 agentType（workflow 源码）", "workflows/dev-spec-review.js",
                "\nconst X = { agentType: 'auditor' }\n", "auditor", offset=1)
    pkg_block = ('\n```json\n{\n  "base": "abc1234",\n  "packages": [\n'
                 '    {"name": "api", "goal": "g", "owned": ["src/**"], "verify": ["t"],\n'
                 '     "owners": ["x"]}\n  ]\n}\n```\n')
    expect_fail(tmp, "JSON 示例中的未知包字段", "skills/parallel-dev/references/work-package.md",
                pkg_block, "owners", offset=6)
    expect_fail(tmp, "JSON 示例中的未知顶层字段", "docs/design.md",
                '\n```json\n{"base": "abc1234", "timeout": 5, "packages": []}\n```\n', "timeout", offset=2)

    # 结构树：在 README「## 结构」目录树开头插入不存在的条目
    repo = copy_repo(tmp, "tree")
    readme = repo / "README.md"
    lines = readme.read_text().splitlines()
    h = next(i for i, ln in enumerate(lines) if re.match(r"^##\s+结构\s*$", ln))
    s = next(i for i in range(h + 1, len(lines)) if lines[i].startswith("```text"))
    lines[s + 1:s + 1] = ["skills/                     注入", "  ghost-skill/              不存在的技能"]
    readme.write_text("\n".join(lines) + "\n")
    r = run_validate(repo)
    out = r.stdout
    check("拦截：结构树里不存在的路径", r.returncode != 0 and f"README.md:{s + 3}" in out and "skills/ghost-skill" in out
          and f"README.md:{s + 2}" not in out, out[-800:])

    # 结构树缩进跳级
    repo = copy_repo(tmp, "tree_indent")
    readme = repo / "README.md"
    lines = readme.read_text().splitlines()
    lines[s + 1:s + 1] = ["hooks/", "      policy-guard.py   跳级"]
    readme.write_text("\n".join(lines) + "\n")
    r = run_validate(repo)
    check("拦截：结构树缩进跳级", r.returncode != 0 and f"README.md:{s + 3}" in r.stdout and "缩进" in r.stdout, r.stdout[-800:])


def test_clean_and_valid_refs(tmp: Path) -> None:
    repo = copy_repo(tmp, "clean")
    r = run_validate(repo)
    check("原样复制通过", r.returncode == 0, (r.stdout + r.stderr)[-800:])

    # 合法引用不得误报；/dev-spec-<name> 也可以是 skills/<name>/SKILL.md
    repo = copy_repo(tmp, "valid")
    # a dedicated fake name: the real dev-spec-dispatch skill exists after integration (incidents: 2026-10-08 集成冲突)
    d = repo / "skills/dev-spec-fakeskill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: dev-spec-fakeskill\ndescription: test\n---\n\n## 1. 一\n")
    append(repo, "docs/design.md", "\n".join([
        "",
        "- `integrate.py apply wt-a wt-b --verify \"pytest --maxfail 1\" --no-owner-check` 与 `integrate.py status --json`",
        "- `bash install.sh update --claude-home /tmp/x`；`bash install.sh --link --no-auto-update --apply`",
        "- `/dev-spec-review`、`/dev-spec-implement`、`/dev-spec-fakeskill`；路径 `~/.claude/dev-spec-backups/` 与 `hooks/dev-spec/x.py` 不是命令",
        "- `parallel-dev` §4、parallel-dev §6–7、`dev-workflow` 技能 §7、`skills/parallel-dev/SKILL.md` §2",
        "- `Agent(subagent_type: \"Explore\")`、`agentType: 'reviewer'`、`subagent_type: \"general-purpose\"`",
        "",
        "```json",
        '{"base": "abc1234", "contract": "x", "packages": [{"name": "a", "goal": "g", "owned": ["a/**"], '
        '"forbidden": [], "setup": "s", "verify": ["v"], "resources": "r", "notes": "n", "effort": "low"}]}',
        "```",
        "",
    ]))
    r = run_validate(repo)
    check("合法引用不误报", r.returncode == 0, (r.stdout + r.stderr)[-1200:])

    # 历史叙述豁免：docs/incidents.md 里的旧命令不报错
    repo = copy_repo(tmp, "incidents")
    append(repo, "docs/incidents.md", "\n- 旧版用 `integrate.py merge-all`。\n")
    r = run_validate(repo)
    check("docs/incidents.md 历史叙述豁免", r.returncode == 0, r.stdout[-600:])


def test_regex_regressions(tmp: Path) -> None:
    # flags after a shell operator belong to the next command, not to the integrate subcommand
    repo = copy_repo(tmp, "ops")
    append(repo, "docs/design.md", "\n- `integrate.py apply wt-a --verify \"pytest\" && git push --force`\n")
    r = run_validate(repo)
    check("回归: && 之后的参数不记到子命令名下", r.returncode == 0, r.stdout[-600:])
    # a skill name immediately followed by CJK text is still parsed as a section reference
    for i, text in enumerate(("parallel-dev技能§9", "`parallel-dev`的§9")):
        repo = copy_repo(tmp, f"cjk{i}")
        line = append(repo, "docs/design.md", f"\n- 见 {text}\n")
        r = run_validate(repo)
        check(f"回归: 识别「{text}」并报不存在的章节", r.returncode != 0 and "§9" in r.stdout, r.stdout[-600:])


def test_select_groups() -> None:
    v = load_validate()
    sel = v.select_groups
    cases = [
        ([], []),
        (["README.md", "docs/design.md"], []),
        (["agents/reviewer.md"], ["validate", "install"]),
        (["global/CLAUDE.md"], ["self_update", "install"]),
        (["skills/dev-workflow/SKILL.md"], ["validate"]),
        (["hooks/policy-guard.py"], ["guard", "parallel", "dispatch"]),
        (["hooks/worktree-guard.py"], ["parallel", "integrate"]),
        (["skills/parallel-dev/scripts/integrate.py"], ["integrate", "dispatch", "validate"]),
        (["workflows/dev-spec-review.js"], ["workflow", "dispatch", "validate"]),
        (["scripts/test_workflows.mjs"], ["workflow", "dispatch"]),
        (["hooks/dev_spec_update.py"], ["self_update", "install"]),
        (["scripts/dev_spec_install.py"], ["self_update", "validate", "install"]),
        (["install.sh"], ["self_update", "validate", "install"]),
        (["skills/dev-spec-dispatch/SKILL.md"], ["dispatch"]),
        (["skills/dev-spec-dispatch/scripts/x.py"], ["dispatch"]),
        (["scripts/test_dispatch.py"], ["dispatch"]),
        (["scripts/test_policy_guard.py"], ["guard"]),
        (["scripts/test_parallel_guards.py"], ["parallel"]),
        (["scripts/test_integrate.py"], ["integrate"]),
        (["scripts/test_self_update.py"], ["self_update"]),
        (["scripts/validate.py"], ["validate"]),
        (["scripts/test_validate.py"], ["validate"]),
        (["hooks/worktree-guard.py", "workflows/dev-spec-implement.js", "README.md"],
         ["parallel", "integrate", "workflow", "dispatch", "validate"]),
        ([".gitignore"], None),
        (["scripts/validate.sh"], None),
        (["README.md", "skills/parallel-dev/scripts/new_tool.py"], None),
    ]
    for files, want in cases:
        got = sel(files)
        check(f"映射 {files} → {want}", got == want, f"实际 {got}")

    # Derived, not hand-listed: every repo file a test script references must select that test's group,
    # so a new cross-file dependency cannot silently fall out of `--changed` (avoids a shared wrong assumption).
    test_group = {"scripts/test_policy_guard.py": "guard", "scripts/test_parallel_guards.py": "parallel",
                  "scripts/test_integrate.py": "integrate", "scripts/test_workflows.mjs": "workflow",
                  "scripts/test_self_update.py": "self_update", "scripts/test_dispatch.py": "dispatch",
                  "scripts/test_validate.py": "validate"}
    tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split())
    tracked |= {str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts
                and ".claude" not in p.parts and "__pycache__" not in p.parts}
    missing = []
    for test, group in test_group.items():
        # only path-constructing code lines (ROOT/repo/src joins, copies, subprocess args); skip comments and docstrings
        src = (ROOT / test).read_text()
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        code = [l for l in src.splitlines() if not l.strip().startswith(("#", "//"))
                and re.search(r"\b(ROOT|root|SRC_\w*|GUARD|TOOL|HOOKS)\b\s*/|join\(ROOT|copy\w*\(|readFileSync|load\(", l)]
        for ref in sorted(set(re.findall(r"[\w./-]+\.(?:py|js|mjs|sh|json|md)", "\n".join(code)))):
            ref = ref.lstrip("./")
            if ref in tracked and ref != test:
                got = sel([ref])
                if got is not None and group not in got:
                    missing.append(f"{ref} → 缺 {group}（{test} 引用了它）")
    check("映射覆盖测试脚本实际引用的全部文件（推导式）", not missing, "; ".join(missing))


def test_changed_git(tmp: Path) -> None:
    repo = copy_repo(tmp, "git")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def g(*a: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a],
                       cwd=repo, env=env, capture_output=True, text=True, check=True)

    def groups(*a: str) -> str:
        r = run_validate(repo, "--changed-groups", *a)
        return r.stdout.strip() if r.returncode == 0 else f"rc={r.returncode} {r.stderr}"

    g("init", "-q")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    check("干净工作区：不选任何测试组", groups() == "", groups())

    sh = subprocess.run(["bash", str(repo / "scripts/validate.sh"), "--changed"], cwd=repo, env=env,
                        capture_output=True, text=True, errors="replace", timeout=300)
    out = sh.stdout
    check("validate.sh --changed 干净时只跑静态检查",
          sh.returncode == 0 and "[static]" in out and "已运行: static\n" in out
          and not re.search(r"^\[(guard|parallel|integrate|workflow|self_update|dispatch|validate|install)\]", out, re.M)
          and "已跳过:" in out and "install" in out.split("已跳过:")[1],
          out[-800:] + sh.stderr[-400:])

    (repo / "hooks/worktree-guard.py").write_text((repo / "hooks/worktree-guard.py").read_text() + "\n# touch\n")
    check("未提交改动纳入", groups() == "parallel integrate", groups())
    (repo / "scripts/test_dispatch.py").write_text("print('ok')\n")
    check("未跟踪文件纳入", groups() == "parallel integrate dispatch", groups())
    g("add", "-A")
    g("commit", "-q", "-m", "change")
    check("提交后相对 HEAD 无改动", groups() == "", groups())
    check("相对 HEAD~1 包含已提交改动", groups("HEAD~1") == "parallel integrate dispatch", groups("HEAD~1"))
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "x/\n")
    check("无法归类时全量", groups() == "all", groups())
    check("无效基线时全量", groups("no-such-ref") == "all", groups("no-such-ref"))
    r = run_validate(repo, "--bogus")
    check("未知参数报用法错误", r.returncode == 2, r.stderr)


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(os.path.realpath(t))
        test_clean_and_valid_refs(tmp)
        test_injections(tmp)
        test_regex_regressions(tmp)
        test_select_groups()
        test_changed_git(tmp)
    failed = [r for r in results if not r[1]]
    for name, ok, detail in failed:
        print(f"FAIL {name}: {detail}")
    print(f"validate: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
