#!/usr/bin/env python3
"""End-to-end tests for hooks/dev_spec_update.py: a bare origin, a source clone, installed homes (copy and link),
and a second clone that pushes new versions. Run: python3 scripts/test_self_update.py
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
results: list[tuple[str, bool, str]] = []
RULE = "global/rules/dev-spec/01-core.md"


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def sh(*args: str, cwd: Path | None = None, env: dict | None = None, inp: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env=env, input=inp)


def git(cwd: Path, *args: str) -> str:
    p = sh("git", *args, cwd=cwd)
    assert p.returncode == 0, f"git {args}: {p.stderr}"
    return p.stdout.strip()


def main() -> int:
    tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="dev-spec-upd-")))
    try:
        origin, src, dev = tmp / "origin.git", tmp / "src", tmp / "dev"
        sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
        # source = current working tree of this repo (tracked + new files), as the version under test
        files = sh("git", "ls-files", "--cached", "--others", "--exclude-standard", cwd=ROOT).stdout.split("\n")
        for f in filter(None, files):
            if (ROOT / f).is_file():
                (src / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / f, src / f)
        for d in (src,):
            git(d, "init", "-q", "-b", "main")
            git(d, "config", "user.email", "t@example.com")
            git(d, "config", "user.name", "t")
            git(d, "add", "-A")
            git(d, "commit", "-qm", "v1")
            git(d, "remote", "add", "origin", str(origin))
            git(d, "push", "-q", "-u", "origin", "main")
        sh("git", "clone", "-q", str(origin), str(dev))
        git(dev, "config", "user.email", "t@example.com")
        git(dev, "config", "user.name", "t")

        def publish(marker: str, extra: dict[str, str] | None = None) -> str:
            p = dev / RULE
            p.write_text(p.read_text() + f"\n<!-- {marker} -->\n")
            for f, body in (extra or {}).items():
                (dev / f).write_text(body)
            git(dev, "commit", "-qam", f"rule {marker}")
            git(dev, "push", "-q", "origin", "main")
            return git(dev, "rev-parse", "HEAD")

        def install(home: Path, *flags: str) -> subprocess.CompletedProcess:
            return sh(sys.executable, str(src / "scripts/dev_spec_install.py"), "install", "--apply",
                      "--skip-version-check", "--claude-home", str(home), *flags)

        def updater(home: Path, now: bool = False, validate: str = "true") -> subprocess.CompletedProcess:
            env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home), "DEV_SPEC_UPDATE_VALIDATE_CMD": validate}
            script = home / "hooks/dev-spec/dev_spec_update.py"
            return sh(sys.executable, str(script), *(["--now"] if now else []), env=env, inp="{}")

        def state(home: Path) -> dict:
            p = home / "dev-spec-update.json"
            return json.loads(p.read_text()) if p.exists() else {}

        # ---------- copy mode ----------
        home = tmp / "home-copy"
        home.mkdir()
        r = install(home, "--copy")
        check("安装成功", r.returncode == 0, r.stdout + r.stderr)
        m = json.loads((home / ".dev-spec-manifest.json").read_text())
        check("默认开启自动更新并记录选项", m["options"]["auto_update"] is True and m["mode"] == "copy", str(m.get("options")))
        settings = json.loads((home / "settings.json").read_text())
        ss = settings["hooks"].get("SessionStart", [])
        check("注册异步 SessionStart 更新 hook", len(ss) == 1 and ss[0]["hooks"][0].get("async") is True
              and "dev_spec_update.py" in ss[0]["hooks"][0]["command"], str(ss))

        v2 = publish("v2")
        r = updater(home)                                            # hook mode: no state yet → runs
        check("hook 模式静默运行（无输出）", r.returncode == 0 and r.stdout == "", r.stdout + r.stderr)
        check("拉取并安装新版本", "v2" in (home / "rules/dev-spec/01-core.md").read_text(), state(home).get("last_result", ""))
        check("源仓库 fast-forward 到远端", git(src, "rev-parse", "HEAD") == v2)
        check("记录更新结果与版本", state(home).get("last_result") == "已更新" and state(home).get("version") == v2[:10], str(state(home)))

        publish("v3")
        before = state(home).get("last_check")
        updater(home)                                                # within interval → throttled
        check("节流：间隔内不再检查", state(home).get("last_check") == before and "v3" not in (home / RULE.replace("global/", "")).read_text())

        good = git(src, "rev-parse", "HEAD")
        r = updater(home, now=True, validate="false")
        check("候选版本校验失败时拒绝更新", state(home).get("last_result", "").startswith("拒绝") and git(src, "rev-parse", "HEAD") == good
              and "v3" not in (home / "rules/dev-spec/01-core.md").read_text(), str(state(home)))
        check("拒绝时清理候选 worktree", git(src, "worktree", "list").count("\n") == 0, git(src, "worktree", "list"))

        (src / "README.md").write_text("local edit\n")
        updater(home, now=True)
        check("源仓库有未提交改动时跳过", "未提交" in state(home).get("last_result", "") and git(src, "rev-parse", "HEAD") == good)
        git(src, "checkout", "--", "README.md")

        (src / "notes.txt").write_text("x\n"); git(src, "add", "notes.txt"); git(src, "commit", "-qm", "local only")
        updater(home, now=True)
        check("本地有未推送提交时跳过", "未推送" in state(home).get("last_result", ""), str(state(home)))
        git(src, "reset", "-q", "--hard", good)

        updater(home, now=True)
        check("正常情况下随后更新成功", state(home).get("last_result") == "已更新" and "v3" in (home / "rules/dev-spec/01-core.md").read_text())
        r = updater(home, now=True)
        check("已是最新", state(home).get("last_result") == "已是最新")

        # install failure after fast-forward → source rolled back
        before_bad = git(src, "rev-parse", "HEAD")
        publish("v4-broken", {"scripts/dev_spec_install.py": "raise SystemExit(3)\n"})
        updater(home, now=True)
        check("新版本安装失败时回退源仓库", state(home).get("last_result", "").startswith("失败") and git(src, "rev-parse", "HEAD") == before_bad,
              str(state(home)))
        git(dev, "revert", "--no-edit", "HEAD"); git(dev, "push", "-q", "origin", "main")

        # signed-only
        install(home, "--copy", "--require-signed")
        publish("v5")
        updater(home, now=True)
        check("要求签名时拒绝未签名提交", "签名" in state(home).get("last_result", ""), str(state(home)))

        # lock held by another session
        (home / "dev-spec-update.json").unlink()
        (home / ".dev-spec-update.lock").mkdir()
        updater(home)
        check("另一会话持锁时不运行", not (home / "dev-spec-update.json").exists())
        (home / ".dev-spec-update.lock").rmdir()

        # turn off
        r = install(home, "--no-auto-update")
        settings = json.loads((home / "settings.json").read_text())
        check("关闭后移除 SessionStart hook 且模式保持 copy", "SessionStart" not in settings.get("hooks", {})
              and json.loads((home / ".dev-spec-manifest.json").read_text())["mode"] == "copy", str(settings.get("hooks", {}).keys()))
        updater(home)
        check("关闭后 hook 模式不做任何事", not (home / "dev-spec-update.json").exists())
        r = install(home)                                            # plain reinstall, no flags
        check("关闭状态在不带参数的重装后保持", r.returncode == 0
              and json.loads((home / ".dev-spec-manifest.json").read_text())["options"]["auto_update"] is False
              and "SessionStart" not in json.loads((home / "settings.json").read_text()).get("hooks", {}), r.stdout[-300:])

        # ---------- link mode ----------
        install(home, "--auto-update")          # re-enable on the copy home so src is in a known state
        home2 = tmp / "home-link"
        home2.mkdir()
        sh("git", "-C", str(src), "fetch", "-q", "origin")
        sh("git", "-C", str(src), "merge", "-q", "--ff-only", "origin/main")
        install(home2, "--link")
        v6 = publish("v6")
        updater(home2, now=True)
        check("link 模式：源仓库更新后软链接即生效", "v6" in (home2 / "rules/dev-spec/01-core.md").read_text()
              and (home2 / "rules/dev-spec").is_symlink() and git(src, "rev-parse", "HEAD") == v6, str(state(home2)))

        # unreachable source
        m2 = json.loads((home2 / ".dev-spec-manifest.json").read_text())
        m2["source"] = str(tmp / "missing")
        (home2 / ".dev-spec-manifest.json").write_text(json.dumps(m2))
        r = updater(home2, now=True)
        check("源不可达时跳过且不报错", r.returncode == 0 and "不可达" in state(home2).get("last_result", ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    for name, ok, detail in failed:
        print(f"FAIL {name}: {detail[:400]}")
    print(f"self-update: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
