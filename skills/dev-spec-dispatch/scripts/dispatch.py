#!/usr/bin/env python3
"""dev-spec dispatch helper: one command from a work-package plan to /dev-spec-implement args.

  dispatch.py check PLAN [--json]               validate a plan (JSON or Markdown table)
  dispatch.py prepare PLAN [--commit] [--message MSG] [--allow-default-branch] [--out FILE]
                                                check -> checkpoint commit -> integrate.py preflight
                                                -> /dev-spec-implement args (base = HEAD)
  dispatch.py review-args --range A..B|A        /dev-spec-review args sized by the diff

PLAN is either JSON shaped like the /dev-spec-implement args ({contract?, packages: [...]}, base
optional) or a Markdown file with a table whose columns are
包名|目标|负责|禁止|验收|准备|资源|effort (multi-value cells split on ; ； or ，) and an optional
line `契约: <权威来源>`. Exit code 2 means invalid plan or blocked dispatch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_BRANCHES = ("main", "master")
WT_EXCLUDE = ":(top,exclude).claude/worktrees"
INTEGRATE = Path(__file__).resolve().parent.parent.parent / "parallel-dev" / "scripts" / "integrate.py"

# Markdown header -> package key. Chinese names are the template; English keys also accepted.
COLUMNS = {
    "包名": "name", "name": "name",
    "目标": "goal", "goal": "goal",
    "负责": "owned", "owned": "owned",
    "禁止": "forbidden", "forbidden": "forbidden",
    "验收": "verify", "verify": "verify",
    "准备": "setup", "setup": "setup",
    "资源": "resources", "resources": "resources",
    "说明": "notes", "notes": "notes",
    "effort": "effort",
}
LIST_KEYS = {"owned", "forbidden", "verify"}
EMPTY_CELL = {"", "-", "—", "无", "n/a", "N/A"}


class PlanError(Exception):
    pass


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def out(*args: str) -> str:
    p = git(*args)
    return p.stdout.rstrip("\n") if p.returncode == 0 else ""  # keep leading spaces: porcelain columns


# ---------- plan loading ----------

def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def clean(cell: str) -> str:
    c = cell.strip()
    if len(c) >= 2 and c[0] == c[-1] == "`":
        c = c[1:-1].strip()
    return "" if c in EMPTY_CELL else c


def split_items(cell: str) -> list[str]:
    return [i for i in (clean(x) for x in re.split(r"[;；，]", cell)) if i]


def parse_markdown(text: str) -> dict:
    plan: dict = {"packages": []}
    m = re.search(r"^\s*(?:[-*]\s*)?(?:契约|contract)\s*[:：]\s*(.+?)\s*$", text, re.M | re.I)
    if m and clean(m.group(1)):
        plan["contract"] = clean(m.group(1))
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|") or i + 1 >= len(lines) \
                or not re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*", lines[i + 1]):
            i += 1
            continue
        header = [COLUMNS.get(h.strip().lower(), COLUMNS.get(h.strip())) for h in split_row(lines[i])]
        if "name" not in header:
            i += 1
            continue
        i += 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            cells = split_row(lines[i])
            pkg: dict = {}
            for key, cell in zip(header, cells):
                if not key:
                    continue
                if key in LIST_KEYS:
                    pkg[key] = split_items(cell)
                elif clean(cell):
                    pkg[key] = clean(cell)
            for key in ("owned", "verify"):
                pkg.setdefault(key, [])
            if not pkg.get("forbidden"):
                pkg.pop("forbidden", None)
            pkg.setdefault("name", "")
            pkg.setdefault("goal", "")
            plan["packages"].append(pkg)
            i += 1
        return plan
    raise PlanError("Markdown 中没有找到含「包名」列的工作包表格")


def load_plan(path: str) -> dict:
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise PlanError(f"无法读取计划文件：{e}")
    if path.lower().endswith(".json") or text.lstrip().startswith("{"):
        try:
            plan = json.loads(text)
        except ValueError as e:
            raise PlanError(f"JSON 无效：{e}")
        if not isinstance(plan, dict):
            raise PlanError("JSON 计划必须是对象 {contract?, packages: [...]}")
        return plan
    return parse_markdown(text)


# ---------- validation (mirrors workflows/dev-spec-implement.js validate()) ----------

def prefix_of(glob: str) -> str:
    """Literal directory prefix, same as prefixOf() in the implement workflow."""
    p = re.sub(r"^(\./)+", "", str(glob).strip())
    p = re.sub(r"/+$", "", p)
    m = re.search(r"[*?\[]", p)
    if not m:
        return p
    cut = p.rfind("/", 0, m.start())
    return "" if cut < 0 else p[:cut]


def overlaps(g1: str, g2: str) -> bool:
    p1, p2 = prefix_of(g1), prefix_of(g2)
    return p1 == "" or p2 == "" or p1 == p2 or p1.startswith(p2 + "/") or p2.startswith(p1 + "/")


def str_list(v) -> list[str]:
    return [x for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []


def validate(plan: dict) -> dict:
    errors, hints = [], []
    pkgs = plan.get("packages")
    if not isinstance(pkgs, list) or not pkgs:
        errors.append("packages 不能为空")
        pkgs = []
    names = set()
    for idx, p in enumerate(pkgs):
        if not isinstance(p, dict):
            errors.append(f"第 {idx + 1} 个包不是对象")
            continue
        n = p.get("name") or f"#{idx + 1}"
        if not p.get("name") or p.get("name") in names:
            errors.append(f"包名缺失或重复：{p.get('name') or f'第 {idx + 1} 个包'}")
        names.add(p.get("name"))
        if not isinstance(p.get("goal"), str) or not p["goal"].strip():
            errors.append(f"{n}: 缺少 goal")
        for key, msg in (("owned", "owned 不能为空"), ("verify", "至少一条 verify 验收命令")):
            v = p.get(key)
            if not isinstance(v, list) or not v:
                errors.append(f"{n}: {msg}")
            elif len(str_list(v)) != len(v):
                errors.append(f"{n}: {key} 只能包含非空字符串")
        if "forbidden" in p and not isinstance(p["forbidden"], list):
            errors.append(f"{n}: forbidden 必须是数组")
        if p.get("effort") is not None and p.get("effort") not in EFFORTS:
            errors.append(f"{n}: effort 必须是 {'/'.join(EFFORTS)} 之一，当前 {p.get('effort')!r}")
    valid = [p for p in pkgs if isinstance(p, dict)]
    for i in range(len(valid)):
        for j in range(i + 1, len(valid)):
            a, b = valid[i], valid[j]
            for g1 in str_list(a.get("owned")):
                for g2 in str_list(b.get("owned")):
                    if overlaps(g1, g2):
                        errors.append(f"归属重叠：{a.get('name')}({g1}) 与 {b.get('name')}({g2})")
    for a in valid:
        for b in valid:
            if a is b:
                continue
            for g1 in str_list(a.get("owned")):
                for g2 in str_list(b.get("forbidden")):
                    if overlaps(g1, g2):
                        hints.append(f"{a.get('name')} 负责 {g1}，而 {b.get('name')} 禁止 {g2}：确认这是有意分工")
    if plan.get("contract") is not None and not isinstance(plan.get("contract"), str):
        errors.append("contract 必须是字符串")
    return {"ok": not errors, "errors": errors, "hints": hints, "packages": len(pkgs)}


def normalized_args(plan: dict, base: str) -> dict:
    keys = ("name", "goal", "owned", "forbidden", "setup", "verify", "resources", "notes", "effort")
    args: dict = {"base": base}
    if plan.get("contract"):
        args["contract"] = plan["contract"]
    args["packages"] = [{k: p[k] for k in keys if k in p and p[k] not in (None, "", [])} for p in plan["packages"]]
    return args


# ---------- commands ----------

def cmd_check(a) -> int:
    try:
        plan = load_plan(a.plan)
    except PlanError as e:
        r = {"ok": False, "errors": [str(e)], "hints": [], "packages": 0}
    else:
        r = validate(plan)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(human_check(r))
    return 0 if r["ok"] else 2


def human_check(r: dict) -> str:
    lines = [f"  错误：{e}" for e in r["errors"]] + [f"  提示：{h}" for h in r["hints"]]
    lines.append(f"计划有效：{r['packages']} 个工作包" if r["ok"] else f"计划无效：{len(r['errors'])} 个错误")
    return "\n".join(lines)


def fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


def cmd_prepare(a) -> int:
    try:
        plan = load_plan(a.plan)
    except PlanError as e:
        return fail(f"计划无效：{e}")
    r = validate(plan)
    if not r["ok"]:
        return fail(human_check(r))
    for h in r["hints"]:
        print(f"提示：{h}", file=sys.stderr)
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return fail("不在 git 仓库内")
    if a.commit and out("status", "--porcelain", "--", ":/", WT_EXCLUDE):
        branch = out("symbolic-ref", "-q", "HEAD")
        if not branch:
            return fail("当前处于 detached HEAD：先切到任务分支再提交检查点")
        name = branch.removeprefix("refs/heads/")
        if name in DEFAULT_BRANCHES and not a.allow_default_branch:
            return fail(f"当前在默认分支 {name}：先建任务分支（git switch -c <task>）再提交检查点，"
                        "确需在默认分支提交时加 --allow-default-branch")
        add = git("add", "-A", "--", ":/", WT_EXCLUDE)  # never stage linked worktrees as gitlinks
        commit = git("commit", "-q", "-m", a.message) if add.returncode == 0 else add
        if commit.returncode != 0:
            return fail(f"提交检查点失败：{(commit.stdout + commit.stderr).strip()}")
        print(f"已提交检查点 {out('rev-parse', '--short', 'HEAD')} 到 {name}", file=sys.stderr)
    head = out("rev-parse", "HEAD")
    if not head:
        return fail("仓库还没有提交，无法确定基线")
    if not INTEGRATE.is_file():
        return fail(f"找不到 parallel-dev 集成脚本：{INTEGRATE}")
    pf = subprocess.run([sys.executable, str(INTEGRATE), "preflight", "--base", head, "--json"],
                        capture_output=True, text=True)
    try:
        report = json.loads(pf.stdout)
    except ValueError:
        return fail(f"preflight 输出无法解析（exit={pf.returncode}）：{(pf.stdout + pf.stderr).strip()}")
    if report.get("blockers") or pf.returncode != 0:
        lines = [f"  阻塞：{b}" for b in report.get("blockers", [])] or [f"  preflight exit={pf.returncode}"]
        return fail("preflight 未通过，未生成 args：\n" + "\n".join(lines))
    if plan.get("base") and plan["base"] != head:
        print(f"提示：计划中的 base {str(plan['base'])[:10]} 已替换为当前 HEAD {head[:10]}", file=sys.stderr)
    text = json.dumps(normalized_args(plan, head), ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n")
        print(f"base {head[:10]}，{len(plan['packages'])} 个工作包，args 已写入 {a.out}")
    else:
        print(text)
    return 0


SECURITY_RX = re.compile(r"auth|login|session|token|secret|credential|crypto|permission|acl|payment|upload"
                         r"|sql|query|exec|shell|hook|\.env", re.I)
PERF_RX = re.compile(r"db|query|cache|index|batch|worker|perf", re.I)
CONTRACT_DIRS = {"type", "types", "typings", "interface", "interfaces", "schema", "schemas", "api", "apis",
                 "proto", "protos", "openapi", "contract", "contracts"}
CONTRACT_FILE_RX = re.compile(r"\.proto$|\.d\.ts$|\.graphql$|schema|openapi|swagger", re.I)


def review_args(range_: str) -> dict:
    rng = range_ if ".." in range_ else f"{range_}..HEAD"
    names = git("diff", "--name-only", "--no-renames", rng)
    stats = git("diff", "--numstat", "--no-renames", rng)
    if names.returncode != 0 or stats.returncode != 0:
        raise PlanError(f"git diff {rng} 失败：{(names.stderr or stats.stderr).strip()}")
    files = [f for f in names.stdout.splitlines() if f]
    added = deleted = 0
    for line in stats.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            added += int(parts[0]) if parts[0].isdigit() else 0   # binary files report "-"
            deleted += int(parts[1]) if parts[1].isdigit() else 0
    total = added + deleted
    tops = sorted({f.split("/", 1)[0] for f in files if "/" in f})
    reasons = {"correctness": "始终包含", "tests": "始终包含"}
    contract_hits = [f for f in files if CONTRACT_DIRS & {s.lower() for s in f.split("/")[:-1]}
                     or CONTRACT_FILE_RX.search(f.rsplit("/", 1)[-1])]
    if contract_hits:
        reasons["contract"] = f"涉及接口/类型/schema：{', '.join(contract_hits[:3])}"
    elif len(tops) >= 3:
        reasons["contract"] = f"跨 {len(tops)} 个顶层目录：{', '.join(tops[:5])}"
    sec = [f for f in files if SECURITY_RX.search(f)]
    if sec:
        reasons["security"] = f"路径命中敏感关键词：{', '.join(sec[:3])}"
    perf = [f for f in files if PERF_RX.search(f)]
    if total > 300:
        reasons["performance"] = f"改动 {total} 行 > 300"
    elif perf:
        reasons["performance"] = f"路径命中性能关键词：{', '.join(perf[:3])}"
    order = ["correctness", "contract", "security", "performance", "tests"]  # LENSES order in the workflow
    return {
        "range": rng,
        "lenses": [l for l in order if l in reasons],
        "finder_effort": "medium" if total < 400 else "high",
        "reasons": reasons,
        "stats": {"files": len(files), "added": added, "deleted": deleted, "top_dirs": tops},
    }


def cmd_review_args(a) -> int:
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return fail("不在 git 仓库内")
    try:
        r = review_args(a.range)
    except PlanError as e:
        return fail(str(e))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="dev-spec 并行派发助手")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="校验工作包计划")
    c.add_argument("plan")
    c.add_argument("--json", action="store_true")
    p = sub.add_parser("prepare", help="校验、提交检查点、preflight 并生成 /dev-spec-implement args")
    p.add_argument("plan")
    p.add_argument("--commit", action="store_true", help="工作区有改动时提交为检查点")
    p.add_argument("--message", default="chore: 并行派发检查点")
    p.add_argument("--allow-default-branch", action="store_true")
    p.add_argument("--out", help="args JSON 写入的文件（默认 stdout）")
    r = sub.add_parser("review-args", help="按改动规模生成 /dev-spec-review args")
    r.add_argument("--range", required=True, help="A..B 或 A（等价 A..HEAD）")
    a = ap.parse_args()
    return {"check": cmd_check, "prepare": cmd_prepare, "review-args": cmd_review_args}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
