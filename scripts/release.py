#!/usr/bin/env python3
"""Cut a release: the only way a version reaches devices on the (default) stable update channel.

  python3 scripts/release.py 1.2.0 [--dry-run]

Gates, all required: X.Y.Z greater than the latest vX.Y.Z tag and not yet used; on `main`, clean,
and identical to origin/main (the commit CI ran on); every GitHub check run on that commit finished
successfully (at least one). Then: annotated tag vX.Y.Z → push tag → GitHub Release whose notes are
generated from Conventional Commit subjects since the previous tag (no hand-written CHANGELOG).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SECTIONS = [("feat", "新功能"), ("fix", "修复"), ("perf", "性能"), ("refactor", "重构"),
            ("docs", "文档"), ("test", "测试"), ("ci", "CI"), ("chore", "杂项")]
OK_CONCLUSIONS = {"success", "skipped", "neutral"}


def run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    p = subprocess.run(list(args), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"{' '.join(args)} 失败：{(p.stdout + p.stderr).strip()}")
    return p


def git(*args: str) -> str:
    p = run("git", *args)
    return p.stdout.rstrip("\n") if p.returncode == 0 else ""


def latest_tag() -> str:
    tags = [t for t in git("tag", "--list", "v*").splitlines() if TAG.match(t)]
    return max(tags, key=lambda t: tuple(int(x) for x in TAG.match(t).groups()), default="")


def release_notes(prev: str) -> str:
    rng = f"{prev}..HEAD" if prev else "HEAD"
    subjects = git("log", "--no-merges", "--format=%s", rng).splitlines()
    groups: dict[str, list[str]] = {}
    for subj in subjects:
        m = re.match(r"^(\w+)(?:\([^)]*\))?!?:\s*(.+)$", subj)
        kind, text = (m.group(1), m.group(2)) if m else ("other", subj)
        key = kind if kind in dict(SECTIONS) else "other"
        groups.setdefault(key, []).append(text)
    lines = []
    for key, title in SECTIONS + [("other", "其他")]:
        if groups.get(key):
            lines.append(f"### {title}")
            lines += [f"- {t}" for t in groups[key]]
            lines.append("")
    return "\n".join(lines).strip() or "（无变更说明）"


def check_runs(repo: str, sha: str, every_attempt: bool = False) -> list[dict] | None:
    """Check runs of a commit (None when the API call fails). every_attempt includes superseded re-run attempts."""
    query = "per_page=100" + ("&filter=all" if every_attempt else "")
    p = run("gh", "api", f"repos/{repo}/commits/{sha}/check-runs?{query}")
    if p.returncode != 0:
        return None
    return json.loads(p.stdout).get("check_runs", [])


def verdict(runs: list[dict] | None) -> tuple[str, str]:
    """(state, message) with state in: success, failed, pending, none, error. A completed failure wins over pending."""
    if runs is None:
        return "error", "读取 CI 状态失败"
    if not runs:
        return "none", "该提交没有任何 CI 运行记录"
    bad = [f"{r['name']}={r.get('conclusion')}" for r in runs
           if r.get("status") == "completed" and r.get("conclusion") not in OK_CONCLUSIONS]
    if bad:
        return "failed", f"CI 未通过：{', '.join(bad)}"
    pending = [r["name"] for r in runs if r.get("status") != "completed"]
    if pending:
        return "pending", f"CI 尚未完成：{', '.join(pending)}"
    if not any(r.get("conclusion") == "success" for r in runs):
        return "failed", "CI 没有成功的检查"
    return "success", f"CI 通过（{len(runs)} 项检查）"


def ci_status(sha: str) -> tuple[bool, str]:
    """CI verdict for `sha`. While a merge commit's own run is still pending (or not yet reported), the verdict of the
    merged branch head (sha^2) is accepted only if ALL of these hold, otherwise the release waits:
      - the two trees are byte-identical, and the branch already contained the main it was merged into
        (so the pull-request run tested exactly this tree, not a merge with a different base);
      - no attempt of the merge commit's own checks has ever failed (a re-run does not launder a failure);
      - every check on the branch head succeeded (no skipped/neutral), covering every check name the merge commit shows."""
    repo = run("gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").stdout.strip()
    if not repo:
        return False, "无法确定 GitHub 仓库（gh 未登录或无远端）"
    own = check_runs(repo, sha)
    state, msg = verdict(own)
    if state == "success":
        return True, msg
    if state not in {"pending", "none"}:
        return False, msg
    merged = git("rev-parse", "--verify", "-q", f"{sha}^2")
    if not merged or git("rev-parse", f"{sha}^{{tree}}") != git("rev-parse", f"{merged}^{{tree}}"):
        return False, msg
    if run("git", "merge-base", "--is-ancestor", f"{sha}^1", merged).returncode != 0:
        return False, msg + "（分支合并前未包含当时的 main，PR 上验证的不是这棵树）"
    history = check_runs(repo, sha, every_attempt=True)
    if verdict(history)[0] in {"failed", "error"}:
        return False, "该提交的 CI 曾经失败（重跑中也不放行）：" + verdict(history)[1]
    theirs = check_runs(repo, merged)
    if verdict(theirs)[0] != "success" or any(r.get("conclusion") != "success" for r in theirs):
        return False, msg
    missing = {r["name"] for r in own or []} - {r["name"] for r in theirs}
    if missing:
        return False, msg + f"（被合并提交上没有这些检查：{', '.join(sorted(missing))}）"
    return True, f"CI 通过（{len(theirs)} 项检查）——取自被合并的提交 {merged[:10]}（文件树完全相同，且该分支已包含合并时的 main）"


def main() -> int:
    ap = argparse.ArgumentParser(description="发布 dev-spec 版本（tag + GitHub Release）")
    ap.add_argument("version", help="X.Y.Z")
    ap.add_argument("--dry-run", action="store_true", help="只检查并打印发布说明")
    a = ap.parse_args()

    problems = []
    if not SEMVER.match(a.version):
        return print(f"版本号必须是 X.Y.Z：{a.version}", file=sys.stderr) or 2
    tag = f"v{a.version}"
    prev = latest_tag()
    if git("tag", "--list", tag):
        problems.append(f"tag {tag} 已存在")
    if prev and tuple(map(int, a.version.split("."))) <= tuple(int(x) for x in TAG.match(prev).groups()):
        problems.append(f"版本必须大于上一个发布 {prev}")
    if git("symbolic-ref", "-q", "--short", "HEAD") != "main":
        problems.append("只能从 main 发布")
    if git("status", "--porcelain"):
        problems.append("工作区有未提交改动")
    run("git", "fetch", "--quiet", "origin", "main", "--tags")
    head = git("rev-parse", "HEAD")
    if head != git("rev-parse", "origin/main"):
        problems.append("HEAD 与 origin/main 不一致：先推送并等 CI 在该提交上通过")
    if not problems:
        ok, msg = ci_status(head)
        print(msg)
        if not ok:
            problems.append(msg)

    notes = release_notes(prev)
    print(f"\n{tag}（上一个：{prev or '无'}）发布说明：\n{notes}\n")
    if problems:
        for p in problems:
            print(f"阻塞：{p}", file=sys.stderr)
        return 2
    if a.dry_run:
        print("dry-run：检查全部通过，未创建 tag")
        return 0
    run("git", "tag", "-a", tag, "-m", f"{tag}\n\n{notes}", check=True)
    push = run("git", "push", "origin", tag)
    if push.returncode != 0:
        run("git", "tag", "-d", tag)
        raise SystemExit(f"推送 tag 失败，已删除本地 tag：{push.stderr.strip()}")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(notes)
    rel = run("gh", "release", "create", tag, "--title", tag, "--notes-file", f.name)
    print(f"已发布 {tag}" + ("" if rel.returncode == 0 else f"（tag 已推送；创建 GitHub Release 失败：{rel.stderr.strip()}）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
