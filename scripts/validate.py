#!/usr/bin/env python3
"""Static checks for the dev-spec source tree. Run via scripts/validate.sh.

  python3 scripts/validate.py                       # 全部静态检查（含交叉引用）
  python3 scripts/validate.py --changed-groups [B]  # 输出相对 B（默认 HEAD）的改动需要跑的测试组；"all" 表示全量
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
RULES_LINE_BUDGET = 200          # always-loaded rules, all files combined
SKILL_LINE_BUDGET = 500          # official guidance per SKILL.md
AGENT_FIELDS = {"name", "description", "tools", "disallowedTools", "model", "permissionMode", "maxTurns",
                "skills", "mcpServers", "hooks", "memory", "background", "omitClaudeMd", "effort",
                "isolation", "color", "initialPrompt", "experimental"}
SKILL_FIELDS = {"name", "description", "when_to_use", "disable-model-invocation", "user-invocable",
                "argument-hint", "arguments", "allowed-tools", "disallowed-tools", "model", "effort",
                "context", "agent", "background", "paths", "shell", "hooks", "metadata", "license",
                "compatibility"}
BUILTIN_AGENT_TYPES = {"Explore", "Plan", "general-purpose"}
# 交叉引用检查不覆盖的历史叙述：事件记录描述的是当时的命令与章节，不随现状改写
XREF_EXEMPT = {"docs/incidents.md"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".tmp"}
errors: list[str] = []


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def repo_files(pattern: str) -> Iterator[Path]:
    """仓库内文件；跳过 .git、缓存与主检出下其他代理的 .claude/worktrees。"""
    for f in sorted(ROOT.rglob(pattern)):
        parts = f.relative_to(ROOT).parts
        if SKIP_DIRS.intersection(parts) or parts[:2] == (".claude", "worktrees"):
            continue
        if f.is_file():
            yield f


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    if not text.startswith("---\n"):
        errors.append(f"{rel(path)}: 缺少 frontmatter")
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        errors.append(f"{rel(path)}: frontmatter 未闭合")
        return {}
    try:  # strict check when PyYAML is available: invalid YAML makes Claude skip the file silently
        import yaml
        yaml.safe_load(text[4:end])
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"{rel(path)}: frontmatter 不是合法 YAML（{e}）")
    fields = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            fields[m.group(1)] = m.group(2)
    return fields


def check_agents() -> None:
    names = set()
    for f in sorted((ROOT / "agents").glob("*.md")):
        fm = frontmatter(f)
        r = rel(f)
        for req in ("name", "description"):
            if not fm.get(req):
                errors.append(f"{r}: 缺少 {req}")
        if unknown := set(fm) - AGENT_FIELDS:
            errors.append(f"{r}: 未知字段 {sorted(unknown)}")
        name = fm.get("name", "")
        if name != f.stem:
            errors.append(f"{r}: name 应与文件名一致")
        if name in names:
            errors.append(f"{r}: 重名 {name}")
        names.add(name)
        if fm.get("model") not in (None, "inherit", "sonnet", "opus", "haiku", "fable") \
                and not str(fm.get("model", "")).startswith("claude-"):
            errors.append(f"{r}: model 值无效")
        if fm.get("effort") not in (None, "low", "medium", "high", "xhigh", "max"):
            errors.append(f"{r}: effort 值无效")


def check_skills() -> None:
    for d in sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir()):
        sk = d / "SKILL.md"
        if not sk.exists():
            errors.append(f"{rel(d)}: 缺少 SKILL.md")
            continue
        r = rel(sk)
        fm = frontmatter(sk)
        if fm.get("name") != d.name:
            errors.append(f"{r}: name 应等于目录名 {d.name}")
        if not fm.get("description"):
            errors.append(f"{r}: 缺少 description")
        if len(fm.get("description", "") + fm.get("when_to_use", "")) > 1536:
            errors.append(f"{r}: description + when_to_use 超过 1536 字符")
        if unknown := set(fm) - SKILL_FIELDS:
            errors.append(f"{r}: 未知字段 {sorted(unknown)}")
        if (n := len(sk.read_text().splitlines())) > SKILL_LINE_BUDGET:
            errors.append(f"{r}: {n} 行，超过 {SKILL_LINE_BUDGET}")


def check_rules_budget() -> None:
    files = sorted((ROOT / "global/rules/dev-spec").glob("*.md"))
    total = sum(len(f.read_text().splitlines()) for f in files)
    if total > RULES_LINE_BUDGET:
        errors.append(f"常驻规则共 {total} 行，超过预算 {RULES_LINE_BUDGET}")
    print(f"  常驻规则 {len(files)} 个文件，共 {total} 行（预算 {RULES_LINE_BUDGET}）")


def check_links() -> None:
    for md in repo_files("*.md"):
        text = re.sub(r"```.*?```", "", md.read_text(), flags=re.S)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            path = (md.parent / target.split("#")[0]).resolve()
            if not path.exists():
                errors.append(f"{rel(md)}: 链接失效 {target}")


def check_json_and_python() -> None:
    for f in repo_files("*.json"):
        try:
            json.loads(f.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"{rel(f)}: JSON 无效 {e}")
    for f in repo_files("*.py"):
        try:
            compile(f.read_text(), str(f), "exec")
        except SyntaxError as e:
            errors.append(f"{rel(f)}: 语法错误 {e}")


def check_no_secrets() -> None:
    sys.path.insert(0, str(ROOT / "hooks"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard", ROOT / "hooks/policy-guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for f in repo_files("*"):
        if f.suffix in {".md", ".json", ".py", ".sh", ".txt"}:
            high, _ = guard.find_secrets(f.read_text(errors="ignore").splitlines())
            if high:
                errors.append(f"{rel(f)}: 疑似密钥 {high}")


# ---------- 交叉引用 ----------

FLAG_RE = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")
QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
INTEGRATE_RE = re.compile(r"integrate\.py\s+([a-z][a-z-]*)")
INSTALL_RE = re.compile(r"install\.sh\b")
WORKFLOW_REF_RE = re.compile(r"(?<![\w./~-])/(dev-spec-[a-z0-9][a-z0-9-]*)(?![\w./-])")
SECTION_RE = re.compile(r"`?(?:skills/)?([a-z][a-z0-9-]*)(?:/SKILL\.md)?`?\s*(?:(?:技能|的)\s*)?§\s*(\d+)(?:\s*[–-]\s*(\d+))?")
AGENT_TYPE_RE = re.compile(r"\b(?:subagent_type|agentType)\s*[:=]\s*[\"']([\w-]+)[\"']")


def md_lines(path: Path) -> Iterator[tuple[int, str, Optional[str]]]:
    """逐行给出 (行号, 内容, 所在围栏代码块的语言；不在代码块内为 None)。"""
    lang: Optional[str] = None
    for i, line in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r"^\s*```(\S*)", line)
        if m:
            lang = (m.group(1) or "text") if lang is None else None
            continue
        yield i, line, lang


def flags_after(line: str, pos: int) -> list[str]:
    """命令之后、同一代码片段内（到下一个反引号或行尾）出现的 --参数；引号内的内容（如 --verify 的值）不算。"""
    seg = QUOTED_RE.sub("", line[pos:].split("`")[0])
    seg = re.split(r"&&|\|\||;|\|", seg)[0]          # flags after a shell operator belong to the next command
    return FLAG_RE.findall(seg)


def cli_help(script: Path, *argv: str) -> Optional[str]:
    try:
        r = subprocess.run([sys.executable, str(script), *argv, "--help"], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        errors.append(f"{rel(script)}: 无法读取命令行参数（{e}）")
        return None
    if r.returncode != 0:
        errors.append(f"{rel(script)}: --help 失败（exit {r.returncode}）{r.stderr.strip()[-200:]}")
        return None
    return r.stdout


def integrate_cli() -> dict[str, set[str]]:
    """integrate.py 的 {子命令: 支持的 --参数}，取自真实 argparse。"""
    script = ROOT / "skills/parallel-dev/scripts/integrate.py"
    top = cli_help(script)
    if top is None:
        return {}
    m = re.search(r"\{([\w,-]+)\}", top)
    if not m:
        errors.append(f"{rel(script)}: 无法从 --help 解析子命令")
        return {}
    out = {}
    for sub in m.group(1).split(","):
        h = cli_help(script, sub)
        out[sub] = set(FLAG_RE.findall(h or "")) | {"--help"}
    return out


def installer_flags() -> set[str]:
    h = cli_help(ROOT / "scripts/dev_spec_install.py")
    return set(FLAG_RE.findall(h or "")) | {"--help"}


def workflow_names() -> set[str]:
    names = set()
    for js in sorted((ROOT / "workflows").glob("*.js")):
        m = re.search(r"export\s+const\s+meta\s*=\s*\{.*?\bname:\s*['\"]([\w-]+)['\"]", js.read_text(), re.S)
        if m:
            names.add(m.group(1))
        else:
            errors.append(f"{rel(js)}: 找不到 meta.name")
    return names


def skill_names() -> set[str]:
    return {d.name for d in (ROOT / "skills").iterdir() if (d / "SKILL.md").is_file()}


def skill_sections(name: str) -> set[int]:
    text = (ROOT / "skills" / name / "SKILL.md").read_text()
    return {int(n) for n in re.findall(r"^##\s+(\d+)\.", text, re.M)}


def agent_names() -> set[str]:
    names = set()
    for f in (ROOT / "agents").glob("*.md"):
        m = re.search(r"^name:\s*(\S+)", f.read_text(), re.M)
        names.add(m.group(1) if m else f.stem)
    return names


def implement_fields() -> tuple[set[str], set[str]]:
    """dev-spec-implement.js 实际读取的 (args 顶层字段, 包字段)。"""
    src = (ROOT / "workflows/dev-spec-implement.js").read_text()
    top = set(re.findall(r"\b(?:a|args)\.([A-Za-z_]\w*)", src))
    pkg = set(re.findall(r"\b(?:p|pkg)\.([A-Za-z_]\w*)", src))
    return top, pkg


def json_blocks(path: Path) -> Iterator[tuple[int, list[str]]]:
    """Markdown 中 json 围栏代码块：(内容首行行号, 行列表)。"""
    start, buf, lang = 0, [], None
    for i, line in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r"^\s*```(\S*)", line)
        if m:
            if lang is None:
                lang, start, buf = m.group(1) or "text", i + 1, []
            else:
                if lang == "json":
                    yield start, buf
                lang = None
        elif lang is not None:
            buf.append(line)


def check_package_examples(md: Path, top_fields: set[str], pkg_fields: set[str]) -> None:
    for start, lines in json_blocks(md):
        body = "\n".join(lines)
        if '"packages"' not in body:
            continue

        def where(key: str) -> str:
            for k, ln in enumerate(lines):
                if f'"{key}"' in ln:
                    return f"{rel(md)}:{start + k}"
            return f"{rel(md)}:{start}"

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            errors.append(f"{rel(md)}:{start + e.lineno - 1}: 含 packages 的 JSON 示例无法解析（{e.msg}）")
            continue
        if not isinstance(data, dict):
            continue
        for key in data:
            if key not in top_fields:
                errors.append(f"{where(key)}: JSON 示例顶层字段 \"{key}\" 未被 workflows/dev-spec-implement.js 读取")
        for pkg in data.get("packages") or []:
            for key in pkg if isinstance(pkg, dict) else []:
                if key not in pkg_fields:
                    errors.append(f"{where(key)}: JSON 示例包字段 \"{key}\" 未被 workflows/dev-spec-implement.js 读取")


def check_structure_tree() -> None:
    """README「## 结构」下 text 代码块里的目录树：每级缩进 2 空格，条目（去掉行尾注释）必须存在。"""
    readme = ROOT / "README.md"
    lines = readme.read_text().splitlines()
    try:
        h = next(i for i, ln in enumerate(lines) if re.match(r"^##\s+结构\s*$", ln))
        s = next(i for i in range(h + 1, len(lines)) if lines[i].startswith("```text"))
    except StopIteration:
        errors.append("README.md: 找不到「## 结构」下的 ```text 目录树")
        return
    stack: list[str] = []
    for i in range(s + 1, len(lines)):
        line = lines[i]
        if line.startswith("```"):
            return
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        level = indent // 2
        if indent % 2 or level > len(stack):
            errors.append(f"README.md:{i + 1}: 目录树缩进错误（应为上一级 +2 空格）")
            continue
        entry = line.split()[0]
        stack = stack[:level] + [entry.rstrip("/")]
        path = "/".join(stack)
        target = ROOT / path
        if not target.exists():
            errors.append(f"README.md:{i + 1}: 目录树条目 {path} 不存在")
        elif entry.endswith("/") and not target.is_dir():
            errors.append(f"README.md:{i + 1}: 目录树条目 {path} 标为目录但不是目录")
    errors.append("README.md: 「## 结构」目录树代码块未闭合")


def check_cross_refs() -> None:
    integ = integrate_cli()
    inst = installer_flags()
    workflows = workflow_names()
    skills = skill_names()
    slash_ok = workflows | {s for s in skills if s.startswith("dev-spec-")}
    sections = {s: skill_sections(s) for s in skills}
    agents = agent_names() | BUILTIN_AGENT_TYPES
    top_fields, pkg_fields = implement_fields()

    for js in sorted((ROOT / "workflows").glob("*.js")):
        for i, line in enumerate(js.read_text().splitlines(), 1):
            for m in AGENT_TYPE_RE.finditer(line):
                if m.group(1) not in agents:
                    errors.append(f"{rel(js)}:{i}: 未知子代理类型 {m.group(1)}")

    for md in repo_files("*.md"):
        r = rel(md)
        if r in XREF_EXEMPT:
            continue
        for i, line, _ in md_lines(md):
            loc = f"{r}:{i}"
            if integ:
                for m in INTEGRATE_RE.finditer(line):
                    sub = m.group(1)
                    if sub not in integ:
                        errors.append(f"{loc}: integrate.py 没有子命令 {sub}（可用：{', '.join(sorted(integ))}）")
                        continue
                    for flag in flags_after(line, m.end()):
                        if flag not in integ[sub]:
                            errors.append(f"{loc}: integrate.py {sub} 不支持参数 {flag}")
            if r == "README.md":
                for m in INSTALL_RE.finditer(line):
                    rest = line[m.end():]
                    if re.match(r"\s+update\b", rest):
                        allowed, what = {"--claude-home"}, "install.sh update"
                    else:
                        allowed, what = inst, "install.sh（scripts/dev_spec_install.py）"
                    for flag in flags_after(line, m.end()):
                        if flag not in allowed:
                            errors.append(f"{loc}: {what} 不支持参数 {flag}")
            for m in WORKFLOW_REF_RE.finditer(line):
                if m.group(1) not in slash_ok:
                    errors.append(f"{loc}: 未知命令 /{m.group(1)}（不是 workflows/*.js 的 meta.name，也不是 skills/<name>/SKILL.md）")
            for m in SECTION_RE.finditer(line):
                name = m.group(1)
                if name not in sections:
                    continue
                lo = int(m.group(2))
                hi = int(m.group(3) or lo)
                for n in range(lo, max(lo, hi) + 1):
                    if n not in sections[name]:
                        errors.append(f"{loc}: {name} 没有 §{n}（SKILL.md 中无「## {n}.」标题）")
            for m in AGENT_TYPE_RE.finditer(line):
                if m.group(1) not in agents:
                    errors.append(f"{loc}: 未知子代理类型 {m.group(1)}（agents/*.md 或内置 {sorted(BUILTIN_AGENT_TYPES)}）")
        check_package_examples(md, top_fields, pkg_fields)
    check_structure_tree()


# ---------- 增量校验：改动 → 测试组 ----------

# validate.sh 中测试组的规范顺序；static 总是运行，不在此列
GROUPS = ("guard", "parallel", "integrate", "workflow", "self_update", "dispatch", "validate", "install")
# 规则按"生产者 → 所有消费它的测试"列出（含跨语言与跨脚本的消费者），先具体后宽泛，命中第一条即停。
# 漏选比多选危险：--changed 报绿而全量失败等于假绿（见 docs/incidents.md 第三轮复核）。
_RULES: list[tuple[str, tuple[str, ...]]] = [
    (r"hooks/policy-guard\.py", ("guard", "parallel", "dispatch")),            # dispatch 复用其密钥检测
    (r"hooks/worktree-guard\.py", ("parallel", "integrate")),                  # integrate 测试经由该 hook 声明归属
    (r"skills/parallel-dev/scripts/integrate\.py", ("integrate", "dispatch", "validate")),  # dispatch 消费 preflight；validate 解析 --help
    (r"workflows/.*", ("workflow", "dispatch", "validate")),                    # dispatch 契约测试；validate 解析 args 字段
    (r"scripts/test_workflows\.mjs", ("workflow", "dispatch")),                # dispatch 经 --validate-args 调用
    (r"hooks/dev_spec_update\.py", ("self_update", "install")),
    (r"scripts/dev_spec_install\.py|install\.sh", ("self_update", "install", "validate")),  # validate 解析安装器 --help
    (r"hooks/hooks\.json", ("self_update", "install")),
    (r"skills/dev-spec-dispatch/.*|scripts/test_dispatch\.py", ("dispatch",)),
    (r"scripts/test_policy_guard\.py", ("guard",)),
    (r"scripts/test_parallel_guards\.py", ("parallel",)),
    (r"scripts/test_integrate\.py", ("integrate",)),
    (r"scripts/test_self_update\.py", ("self_update",)),
    (r"scripts/validate\.py|scripts/test_validate\.py", ("validate",)),
    (r"global/.*", ("install", "self_update")),                                # 安装往返与自更新测试写死了其中文件与内容
    (r"agents/.*", ("install", "validate")),                                   # 安装往返检查 implementer；validate 校验子代理类型
    (r"skills/[^/]+/SKILL\.md", ("validate",)),                               # test_validate 引用真实章节号
    (r".*\.md", ()),                                                          # 其余纯文档：静态检查即可
]


def select_groups(files: list[str]) -> Optional[list[str]]:
    """改动文件 → 需要运行的测试组（规范顺序）；任一文件无法归类时返回 None 表示全量。"""
    want: set[str] = set()
    for f in files:
        for pat, groups in _RULES:
            if re.fullmatch(pat, f):
                want.update(groups)
                break
        else:
            return None
    return [g for g in GROUPS if g in want]


def changed_files(base: str, root: Path = ROOT) -> Optional[list[str]]:
    """相对 base 的已提交与未提交改动，加未跟踪文件；不是 git 仓库或 base 无效时返回 None。"""
    def git(*a: str) -> Optional[str]:
        r = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    diff = git("diff", "--name-only", "--no-renames", base, "--")
    untracked = git("ls-files", "--others", "--exclude-standard")
    if diff is None or untracked is None:
        return None
    return sorted({ln for ln in (diff + untracked).splitlines() if ln})


def changed_groups_main(base: str) -> int:
    files = changed_files(base)
    if files is None:
        print(f"  无法取得相对 {base} 的改动（不是 git 仓库或基线无效），跑全量", file=sys.stderr)
        print("all")
        return 0
    groups = select_groups(files)
    print(f"  相对 {base} 改动 {len(files)} 个文件" + (f"：{', '.join(files[:8])}{' …' if len(files) > 8 else ''}" if files else ""),
          file=sys.stderr)
    if groups is None:
        print("  存在无法归类的改动，跑全量", file=sys.stderr)
    print("all" if groups is None else " ".join(groups))
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--changed-groups"]:
        if len(argv) > 2:
            print("用法: validate.py --changed-groups [<base>]", file=sys.stderr)
            return 2
        return changed_groups_main(argv[1] if len(argv) == 2 else "HEAD")
    if argv:
        print("用法: validate.py [--changed-groups [<base>]]", file=sys.stderr)
        return 2
    check_agents()
    check_skills()
    check_rules_budget()
    check_links()
    check_json_and_python()
    check_no_secrets()
    check_cross_refs()
    for e in errors:
        print("  ERROR", e)
    print("  静态检查:", "通过" if not errors else f"{len(errors)} 个错误")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
