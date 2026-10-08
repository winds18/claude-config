#!/usr/bin/env python3
"""Static checks for the dev-spec source tree. Run via scripts/validate.sh."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

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
errors: list[str] = []


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    if not text.startswith("---\n"):
        errors.append(f"{path.relative_to(ROOT)}: 缺少 frontmatter")
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        errors.append(f"{path.relative_to(ROOT)}: frontmatter 未闭合")
        return {}
    try:  # strict check when PyYAML is available: invalid YAML makes Claude skip the file silently
        import yaml
        yaml.safe_load(text[4:end])
    except ImportError:
        pass
    except Exception as e:
        errors.append(f"{path.relative_to(ROOT)}: frontmatter 不是合法 YAML（{e}）")
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
        rel = f.relative_to(ROOT)
        for req in ("name", "description"):
            if not fm.get(req):
                errors.append(f"{rel}: 缺少 {req}")
        if unknown := set(fm) - AGENT_FIELDS:
            errors.append(f"{rel}: 未知字段 {sorted(unknown)}")
        name = fm.get("name", "")
        if name != f.stem:
            errors.append(f"{rel}: name 应与文件名一致")
        if name in names:
            errors.append(f"{rel}: 重名 {name}")
        names.add(name)
        if fm.get("model") not in (None, "inherit", "sonnet", "opus", "haiku", "fable") \
                and not str(fm.get("model", "")).startswith("claude-"):
            errors.append(f"{rel}: model 值无效")
        if fm.get("effort") not in (None, "low", "medium", "high", "xhigh", "max"):
            errors.append(f"{rel}: effort 值无效")


def check_skills() -> None:
    for d in sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir()):
        sk = d / "SKILL.md"
        rel = sk.relative_to(ROOT)
        if not sk.exists():
            errors.append(f"{d.relative_to(ROOT)}: 缺少 SKILL.md")
            continue
        fm = frontmatter(sk)
        if fm.get("name") != d.name:
            errors.append(f"{rel}: name 应等于目录名 {d.name}")
        if not fm.get("description"):
            errors.append(f"{rel}: 缺少 description")
        if len(fm.get("description", "") + fm.get("when_to_use", "")) > 1536:
            errors.append(f"{rel}: description + when_to_use 超过 1536 字符")
        if unknown := set(fm) - SKILL_FIELDS:
            errors.append(f"{rel}: 未知字段 {sorted(unknown)}")
        if (n := len(sk.read_text().splitlines())) > SKILL_LINE_BUDGET:
            errors.append(f"{rel}: {n} 行，超过 {SKILL_LINE_BUDGET}")


def check_rules_budget() -> None:
    files = sorted((ROOT / "global/rules/dev-spec").glob("*.md"))
    total = sum(len(f.read_text().splitlines()) for f in files)
    if total > RULES_LINE_BUDGET:
        errors.append(f"常驻规则共 {total} 行，超过预算 {RULES_LINE_BUDGET}")
    print(f"  常驻规则 {len(files)} 个文件，共 {total} 行（预算 {RULES_LINE_BUDGET}）")


def check_links() -> None:
    for md in ROOT.rglob("*.md"):
        if ".git" in md.parts:
            continue
        text = re.sub(r"```.*?```", "", md.read_text(), flags=re.S)
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            path = (md.parent / target.split("#")[0]).resolve()
            if not path.exists():
                errors.append(f"{md.relative_to(ROOT)}: 链接失效 {target}")


def check_json_and_python() -> None:
    for f in ROOT.rglob("*.json"):
        if ".git" in f.parts:
            continue
        try:
            json.loads(f.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"{f.relative_to(ROOT)}: JSON 无效 {e}")
    for f in ROOT.rglob("*.py"):
        try:
            compile(f.read_text(), str(f), "exec")
        except SyntaxError as e:
            errors.append(f"{f.relative_to(ROOT)}: 语法错误 {e}")


def check_no_secrets() -> None:
    sys.path.insert(0, str(ROOT / "hooks"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("guard", ROOT / "hooks/policy-guard.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for f in ROOT.rglob("*"):
        if f.is_file() and ".git" not in f.parts and f.suffix in {".md", ".json", ".py", ".sh", ".txt"}:
            high, _ = guard.find_secrets(f.read_text(errors="ignore").splitlines())
            if high:
                errors.append(f"{f.relative_to(ROOT)}: 疑似密钥 {high}")


def main() -> int:
    check_agents()
    check_skills()
    check_rules_budget()
    check_links()
    check_json_and_python()
    check_no_secrets()
    for e in errors:
        print("  ERROR", e)
    print("  静态检查:", "通过" if not errors else f"{len(errors)} 个错误")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
