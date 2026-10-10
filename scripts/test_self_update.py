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
        r = install(home, "--copy", "--channel", "main")         # these scenarios track the branch head
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
        check("记录更新结果与版本", state(home).get("last_result") == "已更新" and v2.startswith(str(state(home).get("version"))), str(state(home)))

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
        install(home2, "--link", "--channel", "main")
        v6 = publish("v6")
        updater(home2, now=True)
        check("link 模式：源仓库更新后软链接即生效", "v6" in (home2 / "rules/dev-spec/01-core.md").read_text()
              and (home2 / "rules/dev-spec").is_symlink() and git(src, "rev-parse", "HEAD") == v6, str(state(home2)))

        # ---------- stable channel: follows release tags only ----------
        src3, home3 = tmp / "src3", tmp / "home-stable"
        sh("git", "clone", "-q", str(origin), str(src3))
        home3.mkdir()
        r = sh(sys.executable, str(src3 / "scripts/dev_spec_install.py"), "install", "--apply", "--skip-version-check",
               "--claude-home", str(home3), "--copy")
        m3 = json.loads((home3 / ".dev-spec-manifest.json").read_text())
        check("stable: 默认通道为 stable 并记录版本", m3["options"]["update_channel"] == "stable" and m3.get("spec_version"), str(m3.get("options")))
        updater(home3, now=True)
        check("stable: 没有发布 tag 时跳过", "没有发布 tag" in state(home3).get("last_result", ""), str(state(home3)))
        git(dev, "fetch", "-q", "origin"); git(dev, "merge", "-q", "--ff-only", "origin/main")
        t1 = publish("rel-1")
        git(dev, "tag", "-a", "v0.1.0", "-m", "v0.1.0"); git(dev, "push", "-q", "origin", "v0.1.0")
        publish("after-rel-1")                                       # on main, not released
        updater(home3, now=True)
        rule3 = (home3 / "rules/dev-spec/01-core.md").read_text()
        check("stable: 更新到发布 tag", state(home3).get("last_result") == "已更新" and "rel-1" in rule3
              and state(home3).get("version") == "v0.1.0", str(state(home3)))
        check("stable: tag 之后的提交不会发布", "after-rel-1" not in rule3 and git(src3, "rev-parse", "HEAD") == t1)
        updater(home3, now=True)
        check("stable: 已在最新 tag", state(home3).get("last_result") == "已是最新", str(state(home3)))
        t2 = publish("rel-2")
        git(dev, "tag", "-a", "v0.2.0", "-m", "v0.2.0"); git(dev, "push", "-q", "origin", "v0.2.0")
        git(dev, "tag", "-a", "v0.10.0", "-m", "x", t1); git(dev, "tag", "v0.3.0-rc1", t2)   # semver order, rc ignored
        git(dev, "push", "-q", "origin", "v0.10.0", "v0.3.0-rc1")
        updater(home3, now=True)
        check("stable: 按语义版本取最高（v0.10.0 > v0.2.0），已包含则视为最新",
              state(home3).get("last_result") in {"已是最新", "已更新"} and git(src3, "rev-parse", "HEAD") in {t1, t2}, str(state(home3)))
        git(dev, "tag", "-d", "v0.10.0"); sh("git", "-C", str(dev), "push", "-q", "origin", ":refs/tags/v0.10.0")
        sh("git", "-C", str(src3), "tag", "-d", "v0.10.0")
        updater(home3, now=True)
        check("stable: 新的发布 tag 被应用", "rel-2" in (home3 / "rules/dev-spec/01-core.md").read_text()
              and git(src3, "rev-parse", "HEAD") == t2, str(state(home3)))
        evil = publish("moved-tag")
        git(dev, "tag", "-f", "-a", "v0.2.0", "-m", "moved", evil)
        sh("git", "-C", str(dev), "push", "-q", "-f", "origin", "v0.2.0")
        updater(home3, now=True)
        check("stable: 被移动的发布 tag 不会被跟随", "moved-tag" not in (home3 / "rules/dev-spec/01-core.md").read_text()
              and git(src3, "rev-parse", "HEAD") == t2, str(state(home3)))

        # ---------- rollback / resume (device on stable, currently at v0.2.0 = t2) ----------
        inst3 = lambda *a: sh(sys.executable, str(src3 / "scripts/dev_spec_install.py"), *a, "--claude-home", str(home3))
        r = inst3("rollback", "v0.1.0")
        rule3 = (home3 / "rules/dev-spec/01-core.md").read_text()
        m3 = json.loads((home3 / ".dev-spec-manifest.json").read_text())
        check("rollback: 内容回到目标版本", r.returncode == 0 and "rel-1" in rule3 and "rel-2" not in rule3, r.stdout + r.stderr)
        check("rollback: 源仓库停在目标 tag（detached）", git(src3, "rev-parse", "HEAD") == t1
              and sh("git", "-C", str(src3), "symbolic-ref", "-q", "HEAD").returncode != 0)
        check("rollback: 自动更新已关闭且 SessionStart hook 移除", m3["options"]["auto_update"] is False
              and "SessionStart" not in json.loads((home3 / "settings.json").read_text()).get("hooks", {}))
        (home3 / "dev-spec-update.json").unlink(missing_ok=True)
        updater(home3)
        check("rollback: hook 模式不会自动升级回去", not (home3 / "dev-spec-update.json").exists())
        updater(home3, now=True)
        check("rollback: 即使手动检查，detached 也跳过", "不在有上游的分支上" in state(home3).get("last_result", "")
              and git(src3, "rev-parse", "HEAD") == t1, str(state(home3)))
        d = inst3("doctor", "--skip-version-check")
        check("rollback: doctor 显示回滚状态与恢复方法", "已回滚到 v0.1.0" in d.stdout and "resume" in d.stdout, d.stdout)
        r = inst3("resume")
        m3 = json.loads((home3 / ".dev-spec-manifest.json").read_text())
        check("resume: 切回原分支并重新开启自动更新", r.returncode == 0 and git(src3, "rev-parse", "HEAD") == t2
              and m3["options"]["auto_update"] is True and "rel-2" in (home3 / "rules/dev-spec/01-core.md").read_text()
              and not (home3 / "dev-spec-rollback.json").exists(), r.stdout + r.stderr)
        r = inst3("rollback", "v9.9.9")
        check("rollback: 不存在的 tag 拒绝", r.returncode == 2 and git(src3, "rev-parse", "HEAD") == t2, r.stderr)
        r = inst3("rollback", "main")
        check("rollback: 只接受发布 tag", r.returncode == 2, r.stderr)
        (src3 / "README.md").write_text("local\n")
        r = inst3("rollback", "v0.1.0")
        check("rollback: 源仓库有改动时拒绝", r.returncode == 2 and git(src3, "rev-parse", "HEAD") == t2, r.stderr)
        git(src3, "checkout", "--", "README.md")
        r = sh(sys.executable, str(src / "scripts/dev_spec_install.py"), "rollback", "v0.1.0", "--claude-home", str(home2))
        check("rollback: link 模式（开发机）拒绝", r.returncode == 2 and "link" in r.stderr, r.stderr)

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
