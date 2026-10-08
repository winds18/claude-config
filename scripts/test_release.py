#!/usr/bin/env python3
"""Tests for scripts/release.py with a real bare origin and a fake `gh` on PATH.
Run: python3 scripts/test_release.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts/release.py"
results: list[tuple[str, bool, str]] = []

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
open(log, "a").write(" ".join(a) + "\\n")
if a[:2] == ["repo", "view"]:
    print("owner/repo"); sys.exit(0)
if a and a[0] == "api":
    state = os.environ.get("FAKE_CI", "success")
    runs = {"success": [{"name": "validate (ubuntu)", "status": "completed", "conclusion": "success"},
                        {"name": "validate (macos)", "status": "completed", "conclusion": "success"}],
            "failure": [{"name": "validate (ubuntu)", "status": "completed", "conclusion": "failure"}],
            "pending": [{"name": "validate (ubuntu)", "status": "in_progress", "conclusion": None}],
            "none": [], "skipped": [{"name": "x", "status": "completed", "conclusion": "skipped"}]}[state]
    print(json.dumps({"check_runs": runs})); sys.exit(0)
if a[:2] == ["release", "create"]:
    sys.exit(0)
sys.exit(1)
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def main() -> int:
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="dev-spec-rel-")))
    try:
        origin, repo, bindir = tmp / "origin.git", tmp / "repo", tmp / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(FAKE_GH)
        (bindir / "gh").chmod(0o755)
        log = tmp / "gh.log"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        repo.mkdir()
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@example.com"); g("config", "user.name", "t")
        g("remote", "add", "origin", str(origin))

        def commit(msg: str) -> None:
            (repo / "f.txt").write_text((repo / "f.txt").read_text() + msg + "\n" if (repo / "f.txt").exists() else msg + "\n")
            g("add", "-A"); g("commit", "-qm", msg)

        def rel(*args: str, ci: str = "success") -> subprocess.CompletedProcess:
            env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_CI": ci, "FAKE_GH_LOG": str(log)}
            return subprocess.run([sys.executable, str(TOOL), *args], cwd=repo, capture_output=True, text=True, env=env)

        commit("feat: 初始功能")
        commit("fix(core): 修复边界")
        commit("随手改动")
        g("push", "-q", "-u", "origin", "main")

        r = rel("1.0", "--dry-run")
        check("拒绝非 X.Y.Z 版本号", r.returncode == 2)
        r = rel("0.1.0", "--dry-run")
        check("dry-run：全部通过且不创建 tag", r.returncode == 0 and not g("tag"), r.stdout + r.stderr)
        check("发布说明按类型分组", "### 新功能" in r.stdout and "初始功能" in r.stdout and "### 修复" in r.stdout
              and "### 其他" in r.stdout, r.stdout)
        for state in ("failure", "pending", "none"):
            r = rel("0.1.0", "--dry-run", ci=state)
            check(f"CI {state} 时阻塞", r.returncode == 2 and "CI" in r.stderr, r.stderr)
        r = rel("0.1.0", "--dry-run", ci="skipped")
        check("只有 skipped 没有 success 时阻塞", r.returncode == 2, r.stderr)

        commit("feat: 未推送")
        r = rel("0.1.0", "--dry-run")
        check("HEAD 未推送到 origin/main 时阻塞", r.returncode == 2 and "origin/main" in r.stderr, r.stderr)
        g("push", "-q")
        (repo / "dirty.txt").write_text("x")
        r = rel("0.1.0", "--dry-run")
        check("工作区不干净时阻塞", r.returncode == 2 and "未提交" in r.stderr, r.stderr)
        (repo / "dirty.txt").unlink()
        g("switch", "-q", "-c", "other")
        r = rel("0.1.0", "--dry-run")
        check("不在 main 时阻塞", r.returncode == 2 and "main" in r.stderr, r.stderr)
        g("switch", "-q", "main")

        log.write_text("")
        r = rel("0.1.0")
        remote_tags = subprocess.run(["git", "ls-remote", "--tags", str(origin)], capture_output=True, text=True).stdout
        check("正式发布：创建并推送带注释的 tag", r.returncode == 0 and "refs/tags/v0.1.0" in remote_tags
              and g("cat-file", "-t", "v0.1.0") == "tag", r.stdout + r.stderr)
        check("正式发布：创建 GitHub Release", "release create v0.1.0" in log.read_text(), log.read_text())
        r = rel("0.1.0", "--dry-run")
        check("同一版本不能重复发布", r.returncode == 2 and "已存在" in r.stderr, r.stderr)
        commit("perf: 更快"); g("push", "-q")
        r = rel("0.0.9", "--dry-run")
        check("版本必须大于上一个发布", r.returncode == 2 and "大于" in r.stderr, r.stderr)
        r = rel("0.2.0", "--dry-run")
        check("发布说明只含上个 tag 之后的提交", "更快" in r.stdout and "初始功能" not in r.stdout, r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    for name, _, detail in failed:
        print(f"FAIL {name}: {detail[:400]}")
    print(f"release: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
