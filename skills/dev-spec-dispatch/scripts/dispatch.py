#!/usr/bin/env python3
"""dev-spec dispatch helper: one command from a work-package plan to /dev-spec-implement args.

  dispatch.py check PLAN [--json]               validate a plan (JSON or Markdown table)
  dispatch.py prepare PLAN [--commit] [--message MSG] [--allow-default-branch] [--out FILE]
                                                check -> checkpoint commit -> integrate.py preflight
                                                -> /dev-spec-implement args (base = HEAD)
  dispatch.py prompts ARGS [--package NAME] [--out-dir DIR]
                                                the three prompts per package (case-designer, implementer,
                                                reviewer) for dispatching by hand with the Agent tool
  dispatch.py review-args --range A..B|A [--prompts]
                                                /dev-spec-review args sized by the diff; --prompts prints the
                                                per-lens finder prompts and the verifier prompt instead
  dispatch.py env [--json]                      delivery / isolation facts of this machine

PLAN is either JSON shaped like the /dev-spec-implement args ({contract?, packages: [...]}, base
optional) or a Markdown file with a table whose columns are
包名|目标|负责|禁止|验收|准备|资源|effort (multi-value cells split on ; ； or ，) and an optional
line `契约: <权威来源>`. Exit code 2 means invalid plan or blocked dispatch.
"""

from __future__ import annotations

import argparse
import json
import shutil
import os
import re
import subprocess
import sys
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODELS = ("sonnet", "haiku", "opus", "fable", "inherit")
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
    "模型": "model", "model": "model",
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

def split_outside_code(text: str, seps: str) -> list[str]:
    """Split on any char in `seps` that is outside `code spans` and not backslash-escaped."""
    parts, cur, in_code, i = [], "", False, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in seps:
            cur += text[i + 1]; i += 2; continue
        if c == "`":
            in_code = not in_code
        if c in seps and not in_code:
            parts.append(cur); cur = ""
        else:
            cur += c
        i += 1
    parts.append(cur)
    return parts


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in split_outside_code(s, "|")]


def clean(cell: str) -> str:
    c = cell.strip()
    if len(c) >= 2 and c[0] == c[-1] == "`" and c.count("`") == 2:
        c = c[1:-1].strip()
    if "`" in c:
        raise PlanError(f"单元格条目残留反引号：{cell.strip()!r}（每个条目单独用一对反引号包住，或不用反引号）")
    return "" if c in EMPTY_CELL else c


def split_items(cell: str) -> list[str]:
    return [i for i in (clean(x) for x in split_outside_code(cell, ";；，,")) if i]


def parse_markdown(text: str) -> dict:
    plan: dict = {"packages": []}
    m = re.search(r"^\s*(?:[-*]\s*)?(?:契约|contract)\s*[:：]\s*(.+?)\s*$", text, re.M | re.I)
    if m and clean(m.group(1)):
        plan["contract"] = clean(m.group(1))
    m = re.search(r"^\s*(?:[-*]\s*)?(?:用例设计|case_design)\s*[:：]\s*(\S+)\s*$", text, re.M | re.I)
    if m:
        v = m.group(1).strip().lower()
        if v not in {"是", "否", "true", "false", "yes", "no", "开", "关"}:
            raise PlanError(f"用例设计 只能是 是/否：{m.group(1)!r}")
        plan["case_design"] = v in {"是", "true", "yes", "开"}
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
            if len(cells) != len(header):
                raise PlanError(f"第 {i + 1} 行有 {len(cells)} 列，表头 {len(header)} 列：单元格里的 | 需写成 \\| 或放进反引号")
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
        if p.get("name") is not None and not isinstance(p.get("name"), str):
            errors.append(f"第 {idx + 1} 个包的 name 必须是字符串")
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
        m = p.get("model")
        if m is not None and not (isinstance(m, str) and (m in MODELS or m.startswith("claude-"))):
            errors.append(f"{n}: model 必须是 {'/'.join(MODELS)} 或完整模型 ID（claude-…），当前 {m!r}")
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
    if "case_design" in plan and not isinstance(plan["case_design"], bool):
        errors.append("case_design 必须是 true/false")
    unknown = set(plan) - {"base", "contract", "packages", "case_design"}
    if unknown:
        errors.append(f"未知的顶层字段：{', '.join(sorted(unknown))}（不会传给 workflow）")
    return {"ok": not errors, "errors": errors, "hints": hints, "packages": len(pkgs)}


def normalized_args(plan: dict, base: str) -> dict:
    keys = ("name", "goal", "owned", "forbidden", "setup", "verify", "resources", "notes", "effort", "model")
    args: dict = {"base": base}
    if plan.get("contract"):
        args["contract"] = plan["contract"]
    args["packages"] = [{k: p[k] for k in keys if k in p and p[k] not in (None, "", [])} for p in plan["packages"]]
    if "case_design" in plan:
        args["case_design"] = plan["case_design"]
    return args


GUARD_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "hooks" / "policy-guard.py",          # repo checkout / link install
    Path(__file__).absolute().parents[3] / "hooks" / "dev-spec" / "policy-guard.py",  # copy install (~/.claude)
)


def scan_staged() -> str | None:
    """Secret scan of staged added lines with the dev-spec guard's detectors. Fails closed."""
    guard = next((c for c in GUARD_CANDIDATES if c.is_file()), None)
    if guard is None:
        return "找不到 dev-spec 守卫（policy-guard.py），无法做提交前密钥扫描；确认安全后可加 --skip-secret-scan"
    import importlib.util
    spec = importlib.util.spec_from_file_location("dev_spec_policy_guard", guard)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    diff = out("diff", "--cached", "--no-color", "-U0", "--no-renames")
    added = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    high, generic = mod.find_secrets(added)
    if high or generic:
        what = ", ".join(high) if high else f"疑似硬编码凭证 {len(generic)} 处（例：{generic[0]}）"
        return f"检查点中疑似包含密钥：{what}。已恢复暂存区、未提交；移除后重试（确认是假数据时在该行注明 dev-spec: allow-secret）"
    return None


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
        if not explicit_identity():
            return fail("未显式设置 git 身份（user.name / user.email）：不提交检查点，否则提交会带上自动生成的 用户名@主机名。"
                        "先 git config user.name / user.email")
        saved_index = out("write-tree")              # restore the user's staging if anything below fails
        restore = lambda: git("read-tree", saved_index) if saved_index else git("reset", "-q")
        add = git("add", "-A", "--", ":/", WT_EXCLUDE)  # never stage linked worktrees as gitlinks
        if add.returncode != 0:
            restore()
            return fail(f"暂存失败：{(add.stdout + add.stderr).strip()}")
        staged = out("diff", "--cached", "--name-only", "--no-renames").splitlines()
        if not a.skip_secret_scan:
            # this commit runs inside python, so the PreToolUse guard never sees a `git commit`: scan here
            problem = scan_staged()
            if problem:
                restore()
                return fail(problem)
        commit = git("commit", "-q", "-m", a.message)
        if commit.returncode != 0:
            restore()
            return fail(f"提交检查点失败，已恢复原暂存区：{(commit.stdout + commit.stderr).strip() or 'git commit 非零退出（检查 pre-commit hook）'}")
        print(f"已提交检查点 {out('rev-parse', '--short', 'HEAD')} 到 {name}（{len(staged)} 个文件："
              f"{', '.join(staged[:8])}{' …' if len(staged) > 8 else ''}）", file=sys.stderr)
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


SECURITY_TOKENS = {"auth", "authn", "authz", "authentication", "authorization", "login", "logout", "signin",
                   "session", "sessions", "token", "tokens", "secret", "secrets", "credential", "credentials",
                   "crypto", "permission", "permissions", "acl", "rbac", "oauth", "jwt", "password", "passwords",
                   "payment", "payments", "billing", "upload", "uploads", "sql", "sanitize", "csrf", "cors"}
SECURITY_CONTENT_RX = re.compile(
    r"\b(?:password|passwd|secret|credential|api[_-]?key|access[_-]?token|jwt|oauth|csrf|permission|authoriz\w*|"
    r"authenticat\w*|is_admin|role|acl|subprocess|os\.system|shell\s*=\s*True|eval\(|exec\(|pickle\.loads|"
    r"yaml\.load\(|innerHTML|dangerouslySetInnerHTML|request\.(?:args|form|json|files)|req\.(?:body|query|params)|"
    r"raw\s*sql|execute\(|cursor\.)", re.I)
PERF_TOKENS = {"db", "database", "query", "queries", "cache", "caching", "batch", "worker", "workers", "perf",
               "performance", "index", "indexes", "migration", "migrations"}
PERF_SKIP_INDEX = {"index"}       # bare index.ts / index.md are entry files, not database indexes
CONTRACT_DIRS = {"type", "types", "typings", "interface", "interfaces", "schema", "schemas", "api", "apis",
                 "proto", "protos", "openapi", "contract", "contracts"}
CONTRACT_FILE_RX = re.compile(r"\.proto$|\.d\.ts$|\.graphql$|schema|openapi|swagger", re.I)


def path_tokens(path: str) -> set[str]:
    return {t for t in re.split(r"[/._\-]+", path.lower()) if t}


def stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].split(".", 1)[0].lower()


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
                     or stem(f) in CONTRACT_DIRS or CONTRACT_FILE_RX.search(f.rsplit("/", 1)[-1])]
    if contract_hits:
        reasons["contract"] = f"涉及接口/类型/schema：{', '.join(contract_hits[:3])}"
    elif len(tops) >= 3:
        reasons["contract"] = f"跨 {len(tops)} 个顶层目录：{', '.join(tops[:5])}"
    sec = [f for f in files if path_tokens(f) & SECURITY_TOKENS or f.rsplit("/", 1)[-1].startswith(".env")]
    diff = git("diff", "--no-color", "-U0", "--no-renames", rng).stdout
    sec_lines = [l[1:].strip() for l in diff.splitlines()
                 if l.startswith("+") and not l.startswith("+++") and SECURITY_CONTENT_RX.search(l)]
    notes = []
    if sec:
        reasons["security"] = f"路径命中敏感关键词：{', '.join(sec[:3])}"
    elif sec_lines:
        reasons["security"] = f"新增代码命中敏感关键词 {len(sec_lines)} 处（例：{sec_lines[0][:60]}）"
    else:
        notes.append("未选 security：仅基于路径与新增行关键词判断；改动涉及鉴权、外部输入或敏感数据时请手动加入")
    perf = [f for f in files if (path_tokens(f) - (PERF_SKIP_INDEX if stem(f) == "index" else set())) & PERF_TOKENS]
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
        "notes": notes,
        "stats": {"files": len(files), "added": added, "deleted": deleted, "top_dirs": tops},
    }


# ---------- prompts for dispatching by hand ----------
# The same three stages workflows/dev-spec-implement.js runs. Sentences in PARITY must stay literally present in
# the workflow source; scripts/test_dispatch.py checks that, so the two paths cannot drift apart silently.

CASE_ASK = "只列与本包改动真实相关、会让实现出错的场景，5–12 条，按出错代价与可能性排序。"
NO_CHEAT = "不得删测试、弱化断言或跳过错误来通过"
REVIEW_FOCUS = "重点：是否实现目标、是否与契约及其消费者一致、失败路径与边界、是否删测试或弱化断言、自报检查是否可信。"
REVIEW_SCALE = "严格度与包的规模相称：目标、契约与验收没有要求的属性不构成 fix-needed。"
FINDER_RULE = "每条发现必须给出可复现的触发条件与错误结果；风格偏好、泛泛的\"建议加测试\"不算。"
VERIFIER_ROLE = "你是对抗验证者：尽力证伪下面这条复核发现，只有在无法证伪时才判 confirmed。"
VERIFIER_METHOD = "方法：阅读真实代码路径；能用只读命令或现有测试验证的就去验证。证据不足时判 uncertain，不要猜。"
LENSES = {   # lens -> (agent type, question); same text as LENSES in workflows/dev-spec-review.js
    "correctness": ("reviewer", "正确性与回归：边界条件、空值/异常路径、并发与状态、错误处理是否吞错"),
    "contract": ("reviewer", "契约一致性：接口/类型/schema 变化后，所有生产者与消费者、文档、测试是否同步；错误语义是否一致"),
    "security": ("security-reviewer", "安全：鉴权与越权、注入、敏感数据进入日志/URL/提交、密钥硬编码、危险默认值"),
    "performance": ("reviewer", "性能与复杂度：N+1、重复扫描或 I/O、无界内存/并发、复杂度退化，需按实际规模说明影响"),
    "tests": ("reviewer", "验证充分性：改动行为是否有测试覆盖失败路径；是否删测试、弱化断言、跳过错误或用 mock 冒充集成"),
}
PARITY = (CASE_ASK, NO_CHEAT, REVIEW_FOCUS, REVIEW_SCALE, FINDER_RULE, VERIFIER_ROLE, VERIFIER_METHOD)


def wants_cases(p: dict, args: dict) -> bool:
    """Proportionality rule shared with the workflow: off globally, or the package is marked mechanical."""
    return args.get("case_design") is not False and p.get("effort") != "low"


def package_prompts(p: dict, args: dict) -> dict:
    """{"case": str | None, "implement": str, "review": str, "agent": {...}} for one package."""
    base, contract, notes = args["base"], args.get("contract"), p.get("notes")
    owned, forbidden = p["owned"], p.get("forbidden") or []
    case = None
    if wants_cases(p, args):
        case = "\n".join(filter(None, [
            f"为工作包「{p['name']}」设计对抗用例，供实现者在写代码前先写成测试。",
            f"目标：{p['goal']}",
            f"负责范围：{', '.join(owned)}",
            f"契约权威来源：{contract}" if contract else "",
            f"关键约束：{notes}" if notes else "",
            f"基线提交：{base}（只读查看现有代码与测试：git show {base}:<path>、git grep）",
            CASE_ASK,
            "每条一行：`优先级（high/medium/low）| 场景名 | 前置 | 操作 | 期望 | 防止的错误`。",
        ]))
    decl = json.dumps({"base": base, "owned": owned, "forbidden": forbidden}, ensure_ascii=False)
    sections = [
        f"## 目标\n{p['goal']}",
        f"## 基线\n- 起点提交：{base}（包含共享契约检查点）" + (f"\n- 先执行：{p['setup']}" if p.get("setup") else ""),
        f"## 归属\n- 你负责（可写）：{', '.join(owned)}\n- 禁止修改：{', '.join(forbidden) or '归属以外的一切'}\n"
        f"- 开工第一步：用 Write 工具写 worktree 根目录的 .dev-spec-owner.json，内容严格为：\n  {decl}\n"
        "  返回\"✓ 归属已记录\"即成功；若被拒并提示起点不包含基线，说明契约检查点不可见，不要开工，按 blocked 回报并写明原因。"
        + (f"\n- 运行资源：{p['resources']}" if p.get("resources") else ""),
        ("## 契约\n" + "\n".join(filter(None, [f"- 权威来源：{contract}" if contract else "",
                                                 f"- 关键约束：{notes}" if notes else ""]))) if contract or notes else "",
        "## 授权\n- 可以：在当前 worktree 分支本地提交\n- 禁止：push、修改归属外文件、用 Bash 写文件绕过 hook",
        ("## 先写成测试的场景（对抗用例）\n先把下列场景写成测试并确认它们在实现前失败（或说明为何不适用），再实现：\n"
         "<粘贴 case-designer 的输出；没有用例时删掉本节>") if case else "",
        "## 验收\n" + "\n".join(f"- {c}" for c in p["verify"]) + f"\n- {NO_CHEAT}",
        "## 回报\n第一行 `STATUS: done|partial|blocked`，随后：分支名与最终提交 SHA（git rev-parse HEAD）、改动文件、"
        "实际运行的每条检查命令与真实退出码（失败原样给出）、未验证面、发现的契约问题。",
    ]
    review = "\n".join(filter(None, [
        f"复核工作包「{p['name']}」在分支 <实现者回报的分支> 上的改动：git diff {base}..<分支>",
        f"目标：{p['goal']}",
        f"契约权威来源：{contract}" if contract else "",
        f"关键约束：{notes}" if notes else "",
        "实现者自报的检查与未验证面：<粘贴回报中的对应部分>",
        "对抗用例（逐条核对是否有对应测试；缺失且未说明理由的算 fix-needed）：\n<粘贴同一份用例>" if case else "",
        REVIEW_FOCUS + REVIEW_SCALE + "只报有触发条件的真实问题。",
        "最后一行 `VERDICT: pass` 或 `VERDICT: fix-needed`。",
    ]))
    agent = {"subagent_type": "implementer", "isolation": "worktree", "run_in_background": True, "name": f"impl-{p['name']}"}
    if p.get("model") and p["model"] != "inherit":
        agent["model"] = p["model"]
    if p.get("effort"):
        agent["effort"] = p["effort"]
    return {"case": case, "implement": "\n\n".join(filter(None, sections)), "review": review, "agent": agent}


def review_prompts(r: dict) -> str:
    """Markdown for the by-hand review after integration: one finder per lens, then one verifier per finding."""
    rng = r["range"]
    parts = [f"# 集成后复核（手动）：{rng}",
             "同一条消息内并发派出下列发现者（只读）；回收后合并同一文件相邻行的重复项，"
             "再为每条候选各派一个验证者。只处理判为 confirmed 的；uncertain 自己核实；一轮为止，分歧由主会话裁决。"]
    for lens in r["lenses"]:
        agent_type, ask = LENSES[lens]
        parts.append(f"## 发现 · {lens}（{agent_type}，effort {r['finder_effort']}；入选理由：{r['reasons'][lens]}）\n\n"
                     f"只读复核 git diff {rng}（先 git diff --stat {rng} 了解范围，再读受影响的调用方与被调用方）。\n"
                     f"本轮只看一个视角——{ask}。\n{FINDER_RULE}没有发现就明确说没有。\n"
                     "每条一行：`严重度（critical/high/medium/low）| 文件:行 | 问题 | 触发条件`。")
    parts.append(f"## 验证（reviewer，每条候选一个）\n\n{VERIFIER_ROLE}\n范围：git diff {rng}\n"
                 "发现：<文件:行> [<严重度>] <问题>\n声称的触发条件：<触发条件>\n"
                 f"{VERIFIER_METHOD}\n最后一行 `VERDICT: confirmed|refuted|uncertain`，并给出证据。")
    for n in r["notes"]:
        parts.append(f"> 注意：{n}")
    return "\n\n".join(parts) + "\n"


def cmd_prompts(a) -> int:
    try:
        args = json.loads(Path(a.args).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return fail(f"读不了 args 文件 {a.args}：{e}")
    if not isinstance(args, dict) or not isinstance(args.get("base"), str) or not re.fullmatch(r"[0-9a-f]{7,64}", args["base"]):
        return fail("需要 prepare 生成的 args（含 base 检查点 SHA），不是原始计划")
    r = validate(args)
    if not r["ok"]:
        return fail("args 无效：\n- " + "\n- ".join(r["errors"]))
    pkgs = [p for p in args["packages"] if a.package in (None, p["name"])]
    if not pkgs:
        return fail(f"没有名为 {a.package} 的包")
    docs = {}
    for p in pkgs:
        pr = package_prompts(p, args)
        call = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in pr["agent"].items())
        doc = [f"# 工作包 {p['name']}", "顺序：用例设计（只读，可与其他包并发）→ 实现 → 包级复核。"
               "复核为 fix-needed 时用 SendMessage 交回原实现者修复，复审一轮为止，仍有分歧由主会话裁决。"]
        if pr["case"]:
            doc.append("## 1. 用例设计 — Agent(subagent_type: \"case-designer\")\n\n" + pr["case"])
        else:
            doc.append("## 1. 用例设计 — 跳过（机械性的包或已全局关闭）")
        doc.append(f"## 2. 实现 — Agent({call})\n\n" + pr["implement"])
        doc.append("## 3. 包级复核 — Agent(subagent_type: \"reviewer\")\n\n" + pr["review"])
        docs[p["name"]] = "\n\n".join(doc) + "\n"
    if a.out_dir:
        d = Path(a.out_dir)
        files = {name: re.sub(r"[^\w.-]+", "_", name) + ".prompts.md" for name in docs}
        if len(set(files.values())) != len(files):
            return fail("包名转成文件名后重复，改用 --package 逐个输出：" + ", ".join(sorted(files)))
        d.mkdir(parents=True, exist_ok=True)
        for name, text in docs.items():
            target = d / files[name]
            target.write_text(text, encoding="utf-8")
            print(target)
    else:
        print("\n".join(docs.values()), end="")
    return 0


def explicit_identity() -> bool:
    """Same rule as integrate.py preflight: an identity the user provided, not git's synthesised user@hostname."""
    return all(git("-c", "user.useConfigOnly=true", "var", v).returncode == 0
               for v in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"))


def listening_ports() -> tuple[list[int], str]:
    """(ports, source). Tries ss (Linux), netstat (macOS/BSD: lists every user's sockets without root), then lsof
    (current user only without root). A tool that is missing or fails falls through; source is "" when none worked."""
    def run_tool(cmd: list[str]) -> str | None:
        if not shutil.which(cmd[0]):
            return None
        p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")   # process names may not be UTF-8
        return p.stdout if p.returncode == 0 else None

    text = run_tool(["ss", "-ltnH"])
    if text is not None:
        cols = (line.split() for line in text.splitlines())
        return sorted({int(c[3].rsplit(":", 1)[-1]) for c in cols if len(c) >= 4 and c[3].rsplit(":", 1)[-1].isdigit()}), "ss"
    text = run_tool(["netstat", "-an", "-p", "tcp"])
    if text is not None:
        found = set()
        for line in text.splitlines():
            c = line.split()
            if len(c) >= 6 and c[-1] == "LISTEN" and c[3].rsplit(".", 1)[-1].isdigit():
                found.add(int(c[3].rsplit(".", 1)[-1]))
        return sorted(found), "netstat"
    text = run_tool(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    if text is not None:
        return sorted({int(m) for m in re.findall(r":(\d+) \(LISTEN\)", text)}), "lsof（仅当前用户的进程）"
    return [], ""


def environment() -> dict:
    """Facts that decide how work can be delivered and isolated on this machine (local or an SSH host)."""
    have = lambda tool: shutil.which(tool) is not None
    ident = explicit_identity()
    gh_ok = have("gh") and subprocess.run(["gh", "auth", "status"], capture_output=True, text=True).returncode == 0
    ports, port_source = listening_ports()
    in_repo = git("rev-parse", "--is-inside-work-tree").returncode == 0
    remote = out("remote", "get-url", "origin") if in_repo else ""
    notes = []
    if not ident:
        notes.append("未显式设置 git 身份：提交会失败，或带上自动生成的 用户名@主机名；先 git config user.name / user.email")
    if not gh_ok:
        notes.append("gh 不可用或未登录：本机不能开 PR / 读 CI / 发布；在这里止于提交与推送，PR 与发布由有 gh 的会话完成")
    if not have("node"):
        notes.append("没有 node：依赖 node 的检查在本机不会运行")
    if not port_source:
        notes.append("无法列出监听端口（ss / netstat / lsof 都不可用或失败）：分配端口前自行确认")
    elif ports:
        notes.append(f"已有 {len(ports)} 个端口在监听（来源 {port_source}）：给各包分配端口时避开它们")
    return {"git_identity": ident, "gh": gh_ok, "node": have("node"), "docker": have("docker"),
            "ssh_session": bool(os.environ.get("SSH_CONNECTION")), "in_repo": in_repo, "origin": remote,
            "listening_ports": ports, "port_source": port_source, "notes": notes}


def cmd_env(a) -> int:
    e = environment()
    if a.json:
        print(json.dumps(e, ensure_ascii=False, indent=2))
    else:
        yn = lambda v: "有" if v else "无"
        print(f"git 身份 {yn(e['git_identity'])} · gh {yn(e['gh'])} · node {yn(e['node'])} · docker {yn(e['docker'])}"
              f" · {'SSH 会话' if e['ssh_session'] else '本机会话'}")
        if e["listening_ports"]:
            print("监听端口: " + ", ".join(map(str, e["listening_ports"][:40])) + (" …" if len(e["listening_ports"]) > 40 else ""))
        for n in e["notes"]:
            print(f"  注意：{n}")
    return 0


def cmd_review_args(a) -> int:
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return fail("不在 git 仓库内")
    try:
        r = review_args(a.range)
    except PlanError as e:
        return fail(str(e))
    print(review_prompts(r) if a.prompts else json.dumps(r, ensure_ascii=False, indent=2), end="" if a.prompts else "\n")
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
    p.add_argument("--skip-secret-scan", action="store_true", help="跳过检查点提交前的密钥扫描（确认安全后才用）")
    e = sub.add_parser("env", help="列出本机的交付与隔离条件：git 身份、gh、node、docker、已占用端口")
    e.add_argument("--json", action="store_true")
    r = sub.add_parser("review-args", help="按改动规模生成 /dev-spec-review args")
    r.add_argument("--range", required=True, help="A..B 或 A（等价 A..HEAD）")
    r.add_argument("--prompts", action="store_true", help="输出手动复核用的各视角提示与验证提示，而不是 JSON")
    m = sub.add_parser("prompts", help="按 prepare 生成的 args 输出每个包的用例设计 / 实现 / 包级复核提示（手动派发用）")
    m.add_argument("args", help="prepare 的 --out 文件")
    m.add_argument("--package", help="只输出这个包")
    m.add_argument("--out-dir", help="每个包写成 <包名>.prompts.md（默认 stdout）")
    a = ap.parse_args()
    return {"check": cmd_check, "prepare": cmd_prepare, "review-args": cmd_review_args, "env": cmd_env, "prompts": cmd_prompts}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
